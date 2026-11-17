"""``query_platform_frictions``: the friction ledger counted, read by an agent.

A session double records the statements and answers fixed rows, so the scope
rule (the platform for an administrator, the caller's own frictions otherwise),
the argument refusals, the tidy rows and the tool's wiring run without a
database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

USER_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def one(self):
        return self._rows[0]

    def all(self):
        return list(self._rows)


class _Db:
    """Answers the totals query, then the grouped rows, then the agent names."""

    def __init__(self, grouped=(), names=()):
        self.statements: list[str] = []
        self._answers = [
            [SimpleNamespace(recorded=3, occurrences=5, undisposed=2)],
            list(grouped),
            list(names),
        ]

    async def execute(self, statement):
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return _Result(self._answers.pop(0))


def _scope(platform: bool):
    from app.services.platform_insights.scope import InsightScope

    return InsightScope(user=SimpleNamespace(id=USER_ID), platform=platform, group_ids=())


def _query(db, platform=True, **args):
    from app.services.platform_insights.frictions import query_platform_frictions

    return asyncio.run(query_platform_frictions(db, _scope(platform), args))


def test_the_totals_alone_without_a_group_by_or_a_bucket():
    db = _Db()
    result = _query(db, period="7d")
    assert result["totals"] == {"recorded": 3, "occurrences": 5, "undisposed": 2}
    assert result["scope"] == "platform"
    assert "rows" not in result
    assert len(db.statements) == 1
    assert "agent_frictions.user_id" not in db.statements[0]


def test_outside_the_administrator_scope_only_the_callers_frictions_are_counted():
    db = _Db()
    result = _query(db, platform=False)
    assert result["scope"] == "self"
    assert "agent_frictions.user_id = " in db.statements[0]


def test_group_by_agent_names_each_agent_and_keeps_its_id():
    grouped = [SimpleNamespace(dimension=str(AGENT_ID), value=4)]
    names = [SimpleNamespace(id=AGENT_ID, name="Developer")]
    result = _query(_Db(grouped, names), group_by="agent")
    assert result["rows"] == [{"bucket": None, "dimension": "Developer", "value": 4, "agent_id": str(AGENT_ID)}]
    assert result["truncated"] is False


def test_bucket_and_group_by_give_tidy_rows_of_the_measure():
    grouped = [
        SimpleNamespace(bucket="2026-11-04", dimension="tool_error", value=2),
        SimpleNamespace(bucket="2026-11-05", dimension="approval_pause", value=1),
    ]
    db = _Db(grouped)
    result = _query(db, group_by="kind", bucket="day", measure="occurrences")
    assert [row["dimension"] for row in result["rows"]] == ["tool_error", "approval_pause"]
    assert result["measure"] == "occurrences"
    assert "sum(agent_frictions.count)" in db.statements[1]


def test_group_by_tool_names_the_empty_tool():
    result = _query(_Db([SimpleNamespace(dimension="", value=1)]), group_by="tool")
    assert result["rows"][0]["dimension"] == "(no tool)"


def test_filters_reach_the_statement():
    db = _Db()
    _query(db, kinds=["tool_error"], classes="harness", agent_ids=[str(AGENT_ID)], undisposed=True)
    sql = db.statements[0]
    for column in ("agent_frictions.kind IN", "agent_frictions.friction_class IN", "agent_frictions.agent_id IN"):
        assert column in sql, column
    assert "agent_frictions.disposed_at IS NULL" in sql


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"group_by": "person"}, "group_by must be one of"),
        ({"bucket": "release"}, "bucket must be one of"),
        ({"measure": "cost"}, "measure must be one of"),
        ({"kinds": ["typo"]}, "unknown kinds"),
        ({"classes": ["nope"]}, "unknown classes"),
        ({"agent_ids": ["not-a-uuid"]}, "agent_ids must be agent UUIDs"),
        ({"period": "custom"}, "period custom needs both"),
    ],
)
def test_bad_arguments_are_refused_by_name(args, fragment):
    from app.services.platform_insights.scope import InsightRefusedError

    with pytest.raises(InsightRefusedError) as refusal:
        _query(_Db(), **args)
    assert fragment in refusal.value.message


def test_the_tool_is_served_by_the_suite_and_returns_json(monkeypatch):
    from app.services.builtin_tools import build_platform_insights_tools
    from app.services.builtin_tools import platform_insights as suite
    from app.tools.base import ToolCall

    defs, _executor = build_platform_insights_tools(str(USER_ID))
    (definition,) = [d for d in defs if d.name == "query_platform_frictions"]
    assert "render_chart" in definition.description
    assert "Admin-gated" in definition.description
    assert definition.input_schema["additionalProperties"] is False

    async def _run(user_id, fn, args):
        return json.dumps(await fn(_Db(), _scope(True), args))

    monkeypatch.setattr(suite, "_run", _run)
    _defs, executor = build_platform_insights_tools(str(USER_ID))
    result = asyncio.run(executor.execute_tool(ToolCall(id="c1", name="query_platform_frictions", arguments={})))
    assert json.loads(result.content)["totals"]["recorded"] == 3
