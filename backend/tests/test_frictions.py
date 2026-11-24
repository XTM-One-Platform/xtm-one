"""The friction ledger: the automatic record of tool errors, the dedupe, and the three tools.

The store is faked at ``async_session`` with an in-memory table so the
recorder's own logic (scope binding, dedupe by normalised evidence, count,
bounds, disposition validation) runs as written; the SQL itself is covered
by the migration and the model.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.models.agent_friction import FRICTION_EVIDENCE_MAX_CHARS
from app.services import frictions
from app.services.builtin_tools import build_friction_tools
from app.tools.base import ToolCall, ToolResult

AGENT = uuid.uuid4()
USER = uuid.uuid4()
RUN = uuid.uuid4()


def test_evidence_hash_ignores_numbers_ids_and_spaces():
    a = frictions.evidence_hash("tool_error", "t", f"timeout after 31 s on {uuid.uuid4()}")
    b = frictions.evidence_hash("tool_error", "t", f"timeout  after 33 s on {uuid.uuid4()}")
    c = frictions.evidence_hash("tool_error", "t", "permission denied")
    assert a == b
    assert a != c
    assert a != frictions.evidence_hash("tool_error", "other", "timeout after 31 s")


def test_evidence_is_bounded():
    assert len(frictions.bound_evidence("x" * 10_000)) == FRICTION_EVIDENCE_MAX_CHARS
    assert frictions.bound_evidence("  short  ") == "short"


def test_make_scope_needs_an_agent():
    assert frictions.make_scope(None) is None
    assert frictions.make_scope("not-a-uuid") is None
    scope = frictions.make_scope(str(AGENT), user_id=str(USER), run_id=RUN, work_item_id="")
    assert scope is not None and scope.agent_id == AGENT and scope.run_id == RUN and scope.work_item_id is None


def test_no_scope_bound_records_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(frictions, "_schedule", lambda coro: (calls.append(coro), coro.close()))
    frictions.note_tool_error("search", "Error: boom")
    assert calls == []


def test_a_bound_scope_records_tool_errors_but_not_the_ledger_tools(monkeypatch):
    calls = []
    monkeypatch.setattr(frictions, "_schedule", lambda coro: (calls.append(coro), coro.close()))
    with frictions.binding_friction_scope(frictions.make_scope(AGENT, run_id=RUN)):
        frictions.note_tool_error("search", "Error: boom")
        frictions.note_tool_error("dispose_friction", "Error: no friction")
    assert len(calls) == 1
    assert frictions.current_friction_scope() is None


def test_the_agent_loop_reports_both_error_shapes(monkeypatch):
    from app.tools import agent_loop

    seen = []
    monkeypatch.setattr(frictions, "note_tool_error", lambda tool, content: seen.append((tool, content)))
    agent_loop._log_tool_result(
        ToolCall(id="1", name="a", arguments={}), ToolResult(tool_call_id="1", content="bad", is_error=True)
    )
    agent_loop._log_tool_result(ToolCall(id="2", name="b", arguments={}), ToolResult(tool_call_id="2", content="Error: x"))
    agent_loop._log_tool_result(ToolCall(id="3", name="c", arguments={}), ToolResult(tool_call_id="3", content="fine"))
    assert seen == [("a", "bad"), ("b", "Error: x")]


# ── A tiny in-memory store standing in for the session ──────────────────────


class _Store:
    def __init__(self):
        self.rows: list = []


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return len(self._rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _matches(row, stmt) -> bool:
    from sqlalchemy.sql.elements import BinaryExpression

    for clause in stmt._where_criteria:
        if not isinstance(clause, BinaryExpression):
            continue
        name = clause.left.key
        right = getattr(clause.right, "value", None)
        op = clause.operator.__name__
        value = getattr(row, name)
        if op == "eq" and value != right:
            return False
        if op == "is_" and value is not None:
            return False
        if op == "ge" and (value is None or value < right):
            return False
    return True


class _Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        rows = [r for r in self.store.rows if _matches(r, stmt)]
        return _Result(rows)

    def add(self, row):
        self.store.rows.append(row)

    async def commit(self):
        return None

    async def refresh(self, row):
        return None


@pytest.fixture
def published(monkeypatch):
    """The platform events raised, captured instead of dispatched."""
    from app.services import platform_events

    seen: list = []

    async def fake_publish(name, data, *, event_id=None):
        seen.append((name, dict(data), event_id))
        return event_id

    monkeypatch.setattr(platform_events, "publish", fake_publish)
    return seen


@pytest.fixture
def store(monkeypatch, published):
    s = _Store()
    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(s))
    return s


def test_the_same_error_twice_in_a_run_is_one_record_with_count_two(store):
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)

    async def go():
        first, created1 = await frictions.record_friction(scope, kind="tool_error", tool="t", evidence="Error: 1 bad")
        second, created2 = await frictions.record_friction(scope, kind="tool_error", tool="t", evidence="Error: 2 bad")
        return first, created1, second, created2

    first, created1, second, created2 = asyncio.run(go())
    assert created1 and not created2
    assert first["id"] == second["id"]
    assert second["count"] == 2
    assert len(store.rows) == 1


def test_an_unknown_kind_is_refused(store):
    scope = frictions.make_scope(AGENT, run_id=RUN)
    with pytest.raises(ValueError):
        asyncio.run(frictions.record_friction(scope, kind="nope"))


def test_the_tools_record_list_and_dispose(store, monkeypatch):
    item_id = str(uuid.uuid4())

    async def fake_get_work_item(ref, *, user_id=None, reader=None):
        return {"id": item_id} if ref == item_id else None

    import app.services.work_item as wi

    monkeypatch.setattr(wi, "get_work_item", fake_get_work_item)
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)

    async def call(name, **arguments):
        return (await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))).content

    async def go():
        recorded = await call("record_friction", what="applied a rule from memory", where="skill section 16")
        fid = store.rows[0].id
        listed = await call("list_frictions", undisposed=True)
        missing = await call("dispose_friction", id=str(fid), friction_class="procedure", disposition="work_item", ref=str(uuid.uuid4()))
        no_ref = await call("dispose_friction", id=str(fid), friction_class="procedure", disposition="skill_edit")
        done = await call("dispose_friction", id=str(fid), friction_class="procedure", disposition="work_item", ref=item_id)
        after = await call("list_frictions", undisposed=True)
        return recorded, listed, missing, no_ref, done, after

    recorded, listed, missing, no_ref, done, after = asyncio.run(go())
    assert recorded.startswith("Recorded")
    assert "1 friction(s)" in listed and "manual on skill section 16" in listed
    assert missing.startswith("Error:") and "not found" in missing
    assert no_ref.startswith("Error:") and "needs ref" in no_ref
    assert done.startswith("Disposed") and item_id in done
    assert store.rows[0].disposed_at is not None
    assert store.rows[0].friction_class == "procedure"
    assert after.startswith("No undisposed friction")


def test_a_harness_friction_refuses_a_skill_or_prompt_edit(store):
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)

    async def call(name, **arguments):
        return (await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))).content

    async def go():
        await call("record_friction", what="a tool answered wrong", where="some_tool")
        fid = str(store.rows[0].id)
        skill = await call(
            "dispose_friction", id=fid, friction_class="harness", disposition="skill_edit", ref="skill X s.3"
        )
        prompt = await call(
            "dispose_friction", id=fid, friction_class="harness", disposition="prompt_edit", ref="prompt Y"
        )
        undisposed = store.rows[0].disposed_at
        kept = await call(
            "dispose_friction", id=fid, friction_class="harness", disposition="shared_state", ref="dev:harness-gaps"
        )
        return skill, prompt, undisposed, kept

    skill, prompt, undisposed, kept = asyncio.run(go())
    assert skill.startswith("Error:") and "harness friction" in skill and "work_item" in skill
    assert prompt.startswith("Error:") and "prompt_edit" in prompt
    assert undisposed is None
    assert kept.startswith("Disposed")
    assert store.rows[0].disposition_kind == "shared_state"


def test_a_work_item_ref_resolves_an_id_prefix(store, monkeypatch):
    item_id = "8588abc0-ebb3-4282-b267-ef4169dccbaa"
    twin_a = "5a5a5a5a-0000-4000-8000-000000000001"
    twin_b = "5a5a5a5a-0000-4000-8000-000000000002"
    items = {item_id: "the carrier", twin_a: "first twin", twin_b: "second twin"}
    seen: list[str] = []

    async def fake_get_work_item(ref, *, user_id=None, reader=None):
        return {"id": ref} if ref in items else None

    async def fake_by_prefix(prefix, *, user_id=None, reader=None, limit=6):
        seen.append(prefix)
        return [{"id": i, "title": t} for i, t in sorted(items.items()) if i.startswith(prefix)][:limit]

    import app.services.work_item as wi

    monkeypatch.setattr(wi, "get_work_item", fake_get_work_item)
    monkeypatch.setattr(wi, "work_items_by_id_prefix", fake_by_prefix)
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)
    friction, _ = asyncio.run(frictions.record_friction(scope, kind="manual", evidence="x"))

    def dispose(ref):
        return asyncio.run(
            frictions.dispose_friction(
                friction["id"],
                agent_id=AGENT,
                user_id=USER,
                friction_class="harness",
                disposition="work_item",
                ref=ref,
            )
        )

    done = dispose("8588abc0")
    assert done["disposition"]["ref"] == item_id
    assert store.rows[0].disposition_ref == item_id
    with pytest.raises(ValueError, match="not found"):
        dispose("deadbeef")
    with pytest.raises(ValueError) as ambiguous:
        dispose("5a5a5a5a")
    assert twin_a in str(ambiguous.value) and twin_b in str(ambiguous.value)
    assert seen == ["8588abc0", "deadbeef", "5a5a5a5a"]
    # The other dispositions take their ref as written: no prefix lookup.
    other = asyncio.run(
        frictions.dispose_friction(
            friction["id"],
            agent_id=AGENT,
            user_id=USER,
            friction_class="harness",
            disposition="shared_state",
            ref="deadbeef",
        )
    )
    assert other["disposition"]["ref"] == "deadbeef" and len(seen) == 3


def test_another_agents_friction_cannot_be_disposed(store):
    other = frictions.make_scope(uuid.uuid4(), user_id=uuid.uuid4(), run_id=uuid.uuid4())
    friction, _ = asyncio.run(frictions.record_friction(other, kind="manual", evidence="x"))
    with pytest.raises(frictions.FrictionNotFoundError):
        asyncio.run(
            frictions.dispose_friction(
                friction["id"], agent_id=AGENT, user_id=USER, friction_class="harness", disposition="accepted"
            )
        )


def test_a_new_friction_raises_one_event_and_a_repeat_none(store, published):
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)

    async def go():
        await frictions.record_friction(scope, kind="tool_error", tool="t", evidence="Error: 1 bad")
        await frictions.record_friction(scope, kind="tool_error", tool="t", evidence="Error: 2 bad")

    asyncio.run(go())
    assert [p[0] for p in published] == ["run.friction_recorded"]
    _, data, event_id = published[0]
    assert data["kind"] == "tool_error" and data["tool"] == "t" and data["run_id"] == str(RUN)
    assert data["participant_agent_ids"] == [str(AGENT)] and data["personal_owner_ids"] == [str(USER)]
    assert event_id == f"friction:{store.rows[0].id}"


class _Run:
    def __init__(self, **kw):
        self.id = kw.get("id", RUN)
        self.agent_id = kw.get("agent_id", AGENT)
        self.user_id = kw.get("user_id", USER)
        self.assignment_id = kw.get("assignment_id")
        self.trigger_payload = kw.get("trigger_payload")
        self.trigger_context = kw.get("trigger_context")
        self.frictions_undisposed = None


def test_a_run_that_leaves_frictions_undisposed_is_counted_and_announced(store, published, monkeypatch):
    run = _Run()
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    async def fake_undisposed(run_id):
        return ids

    monkeypatch.setattr(frictions, "undisposed_of_run", fake_undisposed)
    assert asyncio.run(frictions.settle_run_frictions(run)) == 2
    assert run.frictions_undisposed == 2
    assert [p[0] for p in published] == ["run.completed_with_undisposed_frictions"]
    _, data, event_id = published[0]
    assert data["count"] == 2 and data["friction_ids"] == ids and data["run_id"] == str(RUN)
    assert event_id == f"undisposed-frictions:{RUN}"


def test_a_run_with_every_friction_disposed_is_unchanged(store, published, monkeypatch):
    run = _Run()

    async def fake_undisposed(run_id):
        return []

    monkeypatch.setattr(frictions, "undisposed_of_run", fake_undisposed)
    assert asyncio.run(frictions.settle_run_frictions(run)) == 0
    assert run.frictions_undisposed is None
    assert published == []


def test_an_approval_pause_records_one_friction_per_tool(monkeypatch):
    seen = []
    monkeypatch.setattr(
        frictions, "note_friction", lambda kind, tool="", evidence="", *, scope=None: seen.append((kind, tool, scope))
    )
    item = str(uuid.uuid4())
    run = _Run(trigger_payload={"work_item_id": item})
    frictions.note_approval_pause(run, None, [{"tool_name": "merge_github_pr", "source": "integration"}, "junk"])
    assert [(k, t) for k, t, _ in seen] == [("approval_pause", "merge_github_pr")]
    scope = seen[0][2]
    assert scope.run_id == RUN and scope.agent_id == AGENT and str(scope.work_item_id) == item


def test_a_resumed_turn_is_a_friction_of_the_run(monkeypatch):
    seen = []
    monkeypatch.setattr(frictions, "_schedule", lambda coro: (seen.append(coro), coro.close()))
    frictions.note_turn_resumed(_Run(), cause="its worker was asked to stop", replayed_calls=11, graceful=True)
    assert len(seen) == 1


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(
        frictions, "note_friction", lambda kind, tool="", evidence="", *, scope=None: seen.append((kind, tool, evidence))
    )
    return seen


def test_sandbox_runner_and_trigger_hooks_record_their_kind(monkeypatch):
    seen = _capture(monkeypatch)
    frictions.note_sandbox_failure("code_interpreter", "python sandbox not created: timeout")
    frictions.note_runner_failure("shell", "refused by the runner's permission policy: rm")
    frictions.note_trigger_config_edit("update_assignment", "green check", {"filter": {}, "event": "x"})
    assert [(k, t) for k, t, _ in seen] == [
        ("sandbox_failure", "code_interpreter"),
        ("runner_failure", "shell"),
        ("trigger_filter_hand_edit", "update_assignment"),
    ]
    assert "keys: event, filter" in seen[2][2]


def test_checkout_source_writes_are_grouped_by_checkout():
    manifest = ["xtm/.xtm-checkout.json", "xtm/a.py", "xtm/sub/.xtm-checkout.json", "notes.txt", "other/b.py"]
    found = frictions.checkout_source_writes(
        ["xtm/a.py", "xtm/sub/c.py", "notes.txt", "other/b.py", "xtm/.xtm-checkout.json"], manifest
    )
    assert found == {"xtm": ["xtm/a.py"], "xtm/sub": ["xtm/sub/c.py"]}
    assert frictions.checkout_source_writes(["a.py"], ["report.csv"]) == {}


def test_a_bytecode_cache_is_never_a_checkout_source_write(monkeypatch):
    """An import from code_interpreter writes __pycache__: no friction for it (#9518)."""
    manifest = ["xtm/.xtm-checkout.json", "xtm/pkg/a.py"]
    written = ["xtm/pkg/__pycache__/a.cpython-314.pyc", "xtm/pkg/old.pyc", "xtm/pkg/__pycache__/x.txt"]
    assert frictions.checkout_source_writes(written, manifest) == {}
    seen = _capture(monkeypatch)
    with frictions.binding_friction_scope(frictions.make_scope(AGENT, run_id=RUN)):
        frictions.note_code_interpreter_source_writes(written, manifest)
    assert seen == []


def test_code_interpreter_source_writes_need_a_scope(monkeypatch):
    seen = _capture(monkeypatch)
    manifest = ["xtm/.xtm-checkout.json", "xtm/a.py"]
    frictions.note_code_interpreter_source_writes(["xtm/a.py"], manifest)
    assert seen == []
    with frictions.binding_friction_scope(frictions.make_scope(AGENT, run_id=RUN)):
        frictions.note_code_interpreter_source_writes(["xtm/a.py", "out.csv"], manifest)
    assert [(k, t) for k, t, _ in seen] == [("code_interpreter_source_write", "code_interpreter")]
    assert "1 file(s) of the checkout xtm: xtm/a.py" in seen[0][2]
    assert "catalog keys go through" not in seen[0][2]
    assert "sandbox_edit_file" in seen[0][2]


def test_an_i18n_catalog_is_a_json_file_under_i18n_or_locales():
    assert frictions.is_i18n_catalog("xtm/frontend/src/i18n/lang/en.json")
    assert frictions.is_i18n_catalog("app/public/locales/fr.json")
    assert not frictions.is_i18n_catalog("xtm/frontend/src/i18n/index.ts")
    assert not frictions.is_i18n_catalog("xtm/package.json")
    assert not frictions.is_i18n_catalog("xtm/i18n.json")


def test_a_catalog_write_names_sandbox_json_keys_in_the_hint_and_the_friction(monkeypatch):
    """code_interpreter json.dump of a catalog: the result and the friction name the dedicated tool."""
    manifest = ["xtm/.xtm-checkout.json", "xtm/frontend/src/i18n/lang/en.json", "xtm/a.py"]
    written = ["xtm/frontend/src/i18n/lang/en.json", "xtm/frontend/src/i18n/lang/fr.json", "xtm/a.py"]
    hints = frictions.code_interpreter_write_hints(frictions.checkout_source_writes(written, manifest))
    assert len(hints) == 1
    assert hints[0].startswith(
        "This wrote 2 catalog file(s) of checkout xtm: catalog keys go through sandbox_json_keys("
    )
    assert "'xtm/frontend/src/i18n/lang/en.json', 'xtm/frontend/src/i18n/lang/fr.json'" in hints[0]
    assert "xtm/a.py" not in hints[0]
    seen = _capture(monkeypatch)
    with frictions.binding_friction_scope(frictions.make_scope(AGENT, run_id=RUN)):
        frictions.note_code_interpreter_source_writes(written, manifest)
    assert [(k, t) for k, t, _ in seen] == [("code_interpreter_source_write", "code_interpreter")]
    assert hints[0] in seen[0][2]


def test_a_source_write_names_the_in_place_edit_tools():
    """#11483: a code_interpreter rewrite of a source file points to the tool for each operation."""
    manifest = ["xtm/.xtm-checkout.json", "xtm/a.py"]
    (hint,) = frictions.code_interpreter_write_hints(frictions.checkout_source_writes(["xtm/a.py"], manifest))
    assert hint.startswith("This wrote 1 file(s) of checkout xtm:")
    for tool in ("sandbox_edit_file", "sandbox_write_file", "sandbox_replace_in_files", "sandbox_json_keys"):
        assert tool in hint
    # A catalog outside any checkout is the agent's own file: no hint.
    assert frictions.code_interpreter_write_hints(frictions.checkout_source_writes(["i18n/en.json"], manifest)) == []


def test_the_code_interpreter_result_ends_with_the_catalog_hint():
    from app.services.code_interpreter import format_text_result
    from app.services.sandbox_workspace.run import WorkspaceCall

    call = WorkspaceCall(mounted=True)
    plain = format_text_result({"stdout": "ok", "workspace": call})
    assert "sandbox_json_keys" not in plain
    call.hints.append("This wrote 1 catalog file(s) of checkout xtm: catalog keys go through sandbox_json_keys(...)")
    text = format_text_result({"stdout": "ok", "workspace": call})
    assert text.startswith(plain)
    assert text.endswith(call.hints[0])


def test_the_friction_events_are_in_the_catalog():
    from app.services import platform_events

    assert "run.friction_recorded" in platform_events.PLATFORM_EVENTS
    assert "run.completed_with_undisposed_frictions" in platform_events.PLATFORM_EVENTS
    assert "kind" in platform_events.PLATFORM_EVENTS["run.friction_recorded"]["filter_keys"]


def test_the_tools_are_in_the_coordination_group():
    from app.tools.builtin_suites import builtin_suite_tools

    assert {"record_friction", "list_frictions", "dispose_friction"} <= set(builtin_suite_tools("coordination"))


class _GroupedRows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _SummaryDb:
    def __init__(self, rows):
        self.rows = rows
        self.statements = 0

    async def execute(self, _statement):
        self.statements += 1
        return _GroupedRows(self.rows)


def test_run_friction_summary_counts_records_by_kind_and_the_undisposed():
    db = _SummaryDb([("tool_error", True, 2), ("tool_error", False, 1), ("approval_pause", True, 1)])
    summary = asyncio.run(frictions.run_friction_summary(db, RUN))
    assert summary == {"recorded": 4, "undisposed": 3, "by_kind": {"approval_pause": 1, "tool_error": 3}}
    assert db.statements == 1


def test_run_friction_summaries_group_a_page_of_runs_in_one_read():
    other = uuid.uuid4()
    db = _SummaryDb(
        [(RUN, "turn_resumed", True, 1), (RUN, "trigger_filter_hand_edit", True, 1), (other, "tool_error", False, 2)]
    )
    summaries = asyncio.run(frictions.run_friction_summaries(db, [RUN, str(other), None, "not-a-uuid"]))
    assert summaries == {
        str(RUN): {"recorded": 2, "undisposed": 2, "by_kind": {"trigger_filter_hand_edit": 1, "turn_resumed": 1}},
        str(other): {"recorded": 2, "undisposed": 0, "by_kind": {"tool_error": 2}},
    }
    assert db.statements == 1
    assert asyncio.run(frictions.run_friction_summaries(_SummaryDb([]), [])) == {}
    assert asyncio.run(frictions.run_friction_summaries(object(), [RUN])) == {}


def test_inspect_traces_listing_carries_each_runs_frictions(monkeypatch):
    from types import SimpleNamespace

    from app.services.builtin_tools import traces as traces_tool

    async def _summaries(_db, run_ids):
        assert {str(r) for r in run_ids} == {str(RUN)}
        return {str(RUN): {"recorded": 1, "undisposed": 1, "by_kind": {"sandbox_failure": 1}}}

    monkeypatch.setattr(frictions, "run_friction_summaries", _summaries)
    items = [{"id": "t1", "rollup": {"iterations": 3}}, {"id": "t2"}]
    rows = [(SimpleNamespace(assignment_run_id=RUN),), (SimpleNamespace(assignment_run_id=None),)]
    asyncio.run(traces_tool._annotate_frictions(object(), items, rows))
    expected = {"recorded": 1, "undisposed": 1, "by_kind": {"sandbox_failure": 1}}
    assert items[0]["rollup"] == {"iterations": 3, "frictions": expected}
    assert "rollup" not in items[1], "a chat turn (no assignment run) is left unchanged"


def test_run_friction_summary_is_none_without_frictions_a_run_or_a_readable_store():
    assert asyncio.run(frictions.run_friction_summary(_SummaryDb([]), RUN)) is None
    assert asyncio.run(frictions.run_friction_summary(_SummaryDb([("retry", True, 1)]), "not-a-uuid")) is None
    assert asyncio.run(frictions.run_friction_summary(object(), RUN)) is None


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _RowsDb:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Scalars(self.rows)


def test_run_frictions_lists_the_run_ledger_for_the_run_page():
    from datetime import UTC, datetime

    from app.models.agent_friction import AgentFriction

    at = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
    row = AgentFriction(
        id=uuid.uuid4(),
        agent_id=AGENT,
        run_id=RUN,
        kind="tool_error",
        tool="sandbox_grep",
        evidence="Error: refused",
        count=2,
        friction_class="harness",
        disposition_kind="work_item",
        disposition_ref="item-1",
        recorded_at=at,
        last_seen_at=at,
        disposed_at=at,
    )
    db = _RowsDb([row])
    listed = asyncio.run(frictions.run_frictions(db, RUN))
    assert listed is not None and len(listed) == 1
    assert listed[0]["kind"] == "tool_error" and listed[0]["count"] == 2
    assert listed[0]["disposition"] == {"kind": "work_item", "ref": "item-1"}
    assert len(db.statements) == 1
    assert str(frictions.RUN_PAGE_MAX_FRICTIONS) in str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))


def test_run_frictions_is_none_without_frictions_a_run_or_a_readable_store():
    assert asyncio.run(frictions.run_frictions(_RowsDb([]), RUN)) is None
    assert asyncio.run(frictions.run_frictions(_RowsDb([]), "not-a-uuid")) is None
    assert asyncio.run(frictions.run_frictions(object(), RUN)) is None


def test_the_run_detail_response_carries_the_ledger():
    from app.api.v1.schemas.assignment import AssignmentRunDetailResponse

    fields = AssignmentRunDetailResponse.model_fields
    assert fields["frictions"].default is None
    assert fields["frictions_undisposed"].default is None


# ── Declared probes (expect_error) ─────────────────────────────────────────────────────


def test_expected_error_reason_reads_true_a_reason_or_nothing():
    assert frictions.expected_error_reason(True) == frictions.EXPECTED_ERROR_DEFAULT_REASON
    assert frictions.expected_error_reason("true") == frictions.EXPECTED_ERROR_DEFAULT_REASON
    assert frictions.expected_error_reason("  gate refusal of #8644 ") == "gate refusal of #8644"
    assert len(frictions.expected_error_reason("x" * 500)) == 200
    for value in (False, "false", "", "  ", None, 0, 1, {}):
        assert frictions.expected_error_reason(value) is None


def test_the_agent_loop_strips_the_declaration_before_the_tool_runs():
    from app.tools import agent_loop

    schema = {"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": False}
    tc = ToolCall(id="1", name="get_shared_state", arguments={"key": "k", "expect_error": "no such key"})
    assert agent_loop._strip_expect_error(tc, schema) == "no such key"
    assert tc.arguments == {"key": "k"}
    assert agent_loop.validate_tool_arguments(tc.name, tc.arguments, schema) is None

    plain = ToolCall(id="2", name="get_shared_state", arguments={"key": "k"})
    before = plain.arguments
    assert agent_loop._strip_expect_error(plain, schema) is None
    assert plain.arguments is before

    # expect_error false: stripped, no probe declared, the call behaves as today.
    off = ToolCall(id="3", name="get_shared_state", arguments={"key": "k", "expect_error": False})
    assert agent_loop._strip_expect_error(off, schema) is None
    assert off.arguments == {"key": "k"}


def test_a_tool_that_declares_its_own_expect_error_keeps_it():
    from app.tools import agent_loop

    schema = {"type": "object", "properties": {"expect_error": {"type": "boolean"}}}
    tc = ToolCall(id="1", name="own", arguments={"expect_error": True})
    assert agent_loop._strip_expect_error(tc, schema) is None
    assert tc.arguments == {"expect_error": True}


def test_the_agent_loop_routes_a_probe_by_outcome(monkeypatch):
    from app.tools import agent_loop

    seen = []
    monkeypatch.setattr(frictions, "note_tool_error", lambda tool, content: seen.append(("error", tool)))
    monkeypatch.setattr(
        frictions, "note_expected_error", lambda tool, content, reason: seen.append(("expected", tool, reason))
    )
    monkeypatch.setattr(frictions, "note_probe_passed", lambda tool, reason: seen.append(("passed", tool, reason)))
    agent_loop._log_tool_result(
        ToolCall(id="1", name="a", arguments={}), ToolResult(tool_call_id="1", content="bad", is_error=True), "probe"
    )
    agent_loop._log_tool_result(
        ToolCall(id="2", name="b", arguments={}), ToolResult(tool_call_id="2", content="Error: x"), "probe"
    )
    agent_loop._log_tool_result(ToolCall(id="3", name="c", arguments={}), ToolResult(tool_call_id="3", content="fine"), "probe")
    agent_loop._log_tool_result(
        ToolCall(id="4", name="d", arguments={}), ToolResult(tool_call_id="4", content="bad", is_error=True)
    )
    assert seen == [("expected", "a", "probe"), ("expected", "b", "probe"), ("passed", "c", "probe"), ("error", "d")]


def test_a_declared_probe_error_is_written_disposed_and_raises_no_event(store, published):
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)

    async def go():
        with frictions.binding_friction_scope(scope):
            frictions.note_expected_error("get_shared_state", "Error: no entry probe-nonexistent-key-xyz", "no such key")
            await asyncio.gather(*list(frictions._PENDING))

    asyncio.run(go())
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row.kind == "expected_error" and row.tool == "get_shared_state"
    assert row.friction_class == "platform_acceptable" and row.disposition_kind == "accepted"
    assert row.disposition_ref == "no such key" and row.disposed_at is not None
    assert "no such key" in row.evidence
    assert published == []


def test_a_run_whose_only_frictions_are_declared_probes_raises_nothing(store, published):
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)

    async def go():
        await frictions.record_friction(
            scope, kind="expected_error", tool="t", evidence="e", disposed=("platform_acceptable", "accepted", "r")
        )
        return await frictions.settle_run_frictions(_Run())

    assert asyncio.run(go()) == 0
    assert published == []


def test_a_declared_probe_that_succeeds_is_an_undisposed_mismatch(store, published):
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)

    async def go():
        with frictions.binding_friction_scope(scope):
            frictions.note_probe_passed("get_shared_state", "no such key")
            await asyncio.gather(*list(frictions._PENDING))

    asyncio.run(go())
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row.kind == "probe_passed_unexpectedly" and row.disposed_at is None
    assert "expect_error (no such key)" in row.evidence
    assert [p[0] for p in published] == ["run.friction_recorded"]


def test_a_trigger_config_write_declared_as_a_fix_is_written_disposed(store, published):
    scope = frictions.make_scope(AGENT, user_id=USER, run_id=RUN)

    async def go():
        with frictions.binding_friction_scope(scope):
            frictions.note_trigger_config_edit(
                "update_assignment", "listener", {"filter": {}}, fix_applied="xtm-one #10590"
            )
            frictions.note_trigger_config_edit("update_assignment", "listener", {"filter": {}})
            await asyncio.gather(*list(frictions._PENDING))

    asyncio.run(go())
    assert len(store.rows) == 2
    declared = next(r for r in store.rows if r.disposed_at is not None)
    undeclared = next(r for r in store.rows if r.disposed_at is None)
    assert declared.kind == undeclared.kind == "trigger_filter_hand_edit"
    assert declared.friction_class == "platform_acceptable" and declared.disposition_kind == "accepted"
    assert declared.disposition_ref == "xtm-one #10590" and "fix applied (xtm-one #10590)" in declared.evidence
    assert [p[0] for p in published] == ["run.friction_recorded"]


def test_a_probe_outside_a_scope_or_on_a_ledger_tool_records_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(frictions, "_schedule", lambda coro: (calls.append(coro), coro.close()))
    frictions.note_expected_error("search", "Error: boom", "r")
    with frictions.binding_friction_scope(frictions.make_scope(AGENT, run_id=RUN)):
        frictions.note_expected_error("dispose_friction", "Error: boom", "r")
    assert calls == []
