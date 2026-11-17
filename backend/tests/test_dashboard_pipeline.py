"""Derived data sources and answers in words: closed steps on closed shapes, nothing run.

A derived source passes another source's result through closed steps (where,
sort, head/tail, fields, rename, eval, top, stats); an eval expression is
arithmetic parsed here, never code; every result is closed again. An answer
in words (an assignment's, an agent's, a variable's) is the text itself or
the JSON it holds, in the source's shape, or ``invalid_result``.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.services.dashboard_definition import DefinitionError, validate_definition
from app.services.dashboard_live import MAX_ROWS, LiveError
from app.services.dashboard_pipeline import PipelineError, Step, parse_expression, run_pipeline
from app.services.dashboard_sources import answer_data

STEPS = TypeAdapter(list[Step])

ROWS = {
    "columns": [{"key": "name", "label": "Name"}, {"key": "sev", "label": "Severity"}, {"key": "score", "label": "Score"}],
    "rows": [
        {"name": "VPN gateway", "sev": "critical", "score": "9.8"},
        {"name": "Mail relay", "sev": "high", "score": "7.5"},
        {"name": "Wiki", "sev": "high", "score": "6.1"},
        {"name": "Printer", "sev": "low", "score": "2"},
    ],
    "total": 4,
}


def run(steps: list[dict], shape: str = "rows", data: dict = ROWS) -> tuple[str, dict]:
    return run_pipeline(STEPS.validate_python(steps), shape, data)


def test_where_sort_head_on_rows():
    shape, data = run(
        [
            {"op": "where", "field": "score", "operator": "gte", "value": 6},
            {"op": "sort", "field": "score", "order": "asc"},
            {"op": "head", "count": 2},
        ]
    )
    assert shape == "rows"
    assert [row["name"] for row in data["rows"]] == ["Wiki", "Mail relay"]


@pytest.mark.parametrize(
    ("step", "names"),
    [
        ({"op": "where", "field": "sev", "operator": "eq", "value": "HIGH"}, ["Mail relay", "Wiki"]),
        ({"op": "where", "field": "sev", "operator": "ne", "value": "high"}, ["VPN gateway", "Printer"]),
        ({"op": "where", "field": "name", "operator": "contains", "value": "re"}, ["Mail relay"]),
        ({"op": "where", "field": "sev", "operator": "in", "value": ["low", "critical"]}, ["VPN gateway", "Printer"]),
        ({"op": "where", "field": "score", "operator": "lt", "value": "7"}, ["Wiki", "Printer"]),
        ({"op": "where", "field": "name", "operator": "gt", "value": 1}, []),
        ({"op": "tail", "count": 1}, ["Printer"]),
    ],
)
def test_each_filter(step, names):
    assert [row["name"] for row in run([step])[1]["rows"]] == names


def test_fields_rename_and_eval_shape_the_columns():
    _shape, data = run(
        [
            {"op": "eval", "field": "risk", "expression": "round(score * 10 / (1 + 1), 1)", "label": "Risk"},
            {"op": "rename", "field": "name", "to": "asset", "label": "Asset"},
            {"op": "fields", "keep": ["asset", "risk"]},
        ]
    )
    assert data["columns"] == [{"key": "asset", "label": "Asset"}, {"key": "risk", "label": "Risk"}]
    assert data["rows"][0] == {"asset": "VPN gateway", "risk": "49"}


def test_top_and_stats_change_the_shape():
    assert run([{"op": "top", "field": "sev", "count": 2}]) == (
        "distribution",
        {"items": [{"label": "high", "value": 2.0}, {"label": "critical", "value": 1.0}]},
    )
    assert run([{"op": "stats", "agg": "avg", "field": "score", "by": "sev"}])[1]["items"] == [
        {"label": "critical", "value": 9.8},
        {"label": "high", "value": 6.8},
        {"label": "low", "value": 2.0},
    ]
    assert run([{"op": "stats", "agg": "count"}]) == ("number", {"value": 4.0, "change": None})
    assert run([{"op": "stats", "agg": "max", "field": "score"}])[1]["value"] == 9.8


def test_a_distribution_and_a_series_take_the_steps_that_fit_them():
    distribution = {"items": [{"label": "a", "value": 3}, {"label": "b", "value": 9}, {"label": "c", "value": 1}]}
    assert run([{"op": "sort", "field": "value", "order": "desc"}, {"op": "head", "count": 2}], "distribution", distribution)[1] == {
        "items": [{"label": "b", "value": 9.0}, {"label": "a", "value": 3.0}]
    }
    series = {"series": [{"name": "Findings", "points": [{"x": "2026-10-01", "y": 5}, {"x": "2026-10-08", "y": 7}]}]}
    assert run([{"op": "tail", "count": 1}], "series", series)[1]["series"][0]["points"] == [{"x": "2026-10-08", "y": 7.0}]
    assert run([{"op": "stats", "agg": "last"}], "series", series) == ("number", {"value": 7.0, "change": None})


@pytest.mark.parametrize(
    ("steps", "shape", "fragment"),
    [
        ([{"op": "fields", "keep": ["a"]}], "distribution", "steps.0 \\(fields\\) applies to rows"),
        ([{"op": "where", "field": "x", "value": 1}], "number", "steps.0 applies to rows or a distribution"),
        ([{"op": "stats", "agg": "sum"}], "rows", "stats sum needs a field"),
        ([{"op": "eval", "field": "x", "expression": "__import__('os')"}], "rows", "eval"),
        ([{"op": "head", "count": 1}], "text", "a text result has no steps"),
    ],
)
def test_a_step_that_does_not_apply_says_where(steps, shape, fragment):
    data = {"number": {"value": 1}, "text": {"text": "x"}, "distribution": {"items": []}, "rows": ROWS}[shape]
    with pytest.raises(PipelineError, match=fragment):
        run(steps, shape, data)


@pytest.mark.parametrize(
    "expression",
    ["score ** 2", "score; 1", "open('x')", "a.b", "1 +", "(1", "max(1)", "lambda: 1", "'text'", "score[0]"],
)
def test_eval_is_arithmetic_and_nothing_else(expression):
    with pytest.raises(PipelineError):
        parse_expression(expression)


def test_steps_are_closed_and_bounded():
    with pytest.raises(ValidationError):
        STEPS.validate_python([{"op": "shell", "cmd": "ls"}])
    with pytest.raises(ValidationError):
        STEPS.validate_python([{"op": "head", "count": 0}])
    with pytest.raises(ValidationError):
        STEPS.validate_python([{"op": "where", "field": "a", "operator": "regex", "value": ".*"}])
    many = {"columns": [{"key": "n"}], "rows": [{"n": str(i)} for i in range(500)]}
    assert len(run([{"op": "eval", "field": "m", "expression": "n * 2"}], "rows", many)[1]["rows"]) == MAX_ROWS


@pytest.mark.parametrize(
    ("answer", "shape", "path", "expected"),
    [
        ("All quiet this week.\nNo new KEV.", "text", None, {"text": "All quiet this week.\nNo new KEV."}),
        (
            "Start with LockBit.\n\n[[DASHBOARD:cadf3756-5907-4453-bd2a-9c0c9fca1747]]\n\nThen the campaigns.",
            "text",
            None,
            {"text": "Start with LockBit.\n\nThen the campaigns."},
        ),
        (" 42 ", "number", None, {"value": 42.0, "change": None}),
        ('```json\n{"response": {"open": 37, "change": -5}}\n```', "number", "response.open", {"value": 37.0, "change": None}),
        ('{"value": 37, "change": -5}', "number", None, {"value": 37.0, "change": -5.0}),
        ('[{"label": "High", "value": 3}]', "distribution", None, {"items": [{"label": "High", "value": 3.0}]}),
        (
            '{"r": [{"host": "a", "cve": "CVE-1"}]}',
            "rows",
            "r",
            {"columns": [{"key": "host", "label": "host"}, {"key": "cve", "label": "cve"}], "rows": [{"host": "a", "cve": "CVE-1"}], "total": None},
        ),
        ('{"a": {"b": ["x", "y"]}}', "text", "a.b.1", {"text": "y"}),
        ('Label line\n{"open": 37, "closed": 12}\nThanks.', "number", "open", {"value": 37.0, "change": None}),
        ("Here: [1, 2] and more", "text", "1", {"text": "2"}),
        ('🤖 [AI-generated]\n{"open": 37, "closed": 12}', "number", "open", {"value": 37.0, "change": None}),
    ],
)
def test_an_answer_in_words_reads_as_its_shape(answer, shape, path, expected):
    assert answer_data(answer, shape, path) == expected


@pytest.mark.parametrize(
    ("answer", "shape", "path"),
    [
        ("about forty", "number", None),
        ("not json", "rows", None),
        ('{"a": 1}', "number", "b"),
        ("an {unclosed object", "number", "a"),
        ('{"a": [1]}', "text", "a.5"),
        ('[{"host": "a", "_url": "https://evil.example"}]', "rows", None),
    ],
)
def test_an_answer_that_does_not_fit_is_invalid(answer, shape, path):
    with pytest.raises(LiveError):
        answer_data(answer, shape, path)


def _definition(sources: dict) -> dict:
    return {
        "version": 2,
        "title": "T",
        "dataSources": sources,
        "visualizations": {"v": {"type": "xtm.table", "dataSources": {"primary": next(iter(sources))}}},
        "layout": {
            "tabs": {"items": [{"layoutId": "l", "label": "L"}]},
            "layoutDefinitions": {"l": {"structure": [{"item": "v", "position": {"x": 0, "y": 0, "w": 6, "h": 6}}]}},
        },
    }


BASE = {"type": "static", "options": {"shape": "rows", "data": ROWS}}
AID = "11111111-1111-4111-8111-111111111111"


def test_every_source_type_validates():
    definition = validate_definition(
        _definition(
            {
                "top": {"type": "derived", "options": {"extends": "base", "steps": [{"op": "top", "field": "sev"}], "refresh": "5m"}},
                "base": BASE,
                "kev": {"type": "assignment", "options": {"assignment_id": AID, "shape": "number", "path": "response.kev"}},
                "q": {"type": "ask", "options": {"agent_id": AID, "question": "How many open findings?", "shape": "number"}},
                "var": {"type": "variable", "options": {"variable_id": AID, "refreshType": "interval", "refresh": "1h"}},
            }
        )
    )
    assert definition["dataSources"]["top"]["options"]["refresh"] == "5m"
    assert definition["dataSources"]["var"]["options"]["refreshType"] == "interval"


@pytest.mark.parametrize(
    ("sources", "fragment"),
    [
        ({"d": {"type": "derived", "options": {"extends": "ghost"}}}, "extends names no data source 'ghost'"),
        (
            {"a": {"type": "derived", "options": {"extends": "b"}}, "b": {"type": "derived", "options": {"extends": "a"}}},
            "its chain comes back",
        ),
        (
            {
                **{f"d{i}": {"type": "derived", "options": {"extends": f"d{i + 1}"}} for i in range(5)},
                "d5": BASE,
            },
            "at most 4 derived",
        ),
        ({"v": {"type": "variable", "options": {"variable_id": AID, "refresh": "5s"}}}, "refresh is a duration"),
        ({"v": {"type": "variable", "options": {"variable_id": AID, "refresh": "2d"}}}, "refresh is a duration"),
        ({"a": {"type": "assignment", "options": {"assignment_id": AID, "path": "a..b"}}}, "path is up to"),
        ({"a": {"type": "ask", "options": {"agent_id": AID, "question": ""}}}, "question"),
        ({"a": {"type": "ask", "options": {"agent_id": "not-a-uuid", "question": "q"}}}, "agent_id"),
    ],
)
def test_a_source_is_refused_with_its_field(sources, fragment):
    with pytest.raises(DefinitionError, match=fragment):
        validate_definition(_definition(sources))
