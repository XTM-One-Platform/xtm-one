"""The pure parts of a dashboard's published link (``dashboard_public_links``).

A visitor never receives a link into a platform (the result's ``open_url``, a
row's ``_url``, an item's, an event's, an outcome's or an action's ``link``); the snapshot is read with the page's default tokens; a
question to an agent is recognised through any chain of derived sources; the
definition a visitor receives keeps a canvas's blocks and lines, never its
inputs, and a visualization's options lose their link.
"""

from __future__ import annotations

import pytest

from app.services.dashboard_public_links import _without_links, asks_an_agent, public_definition
from app.services.dashboard_tokens import default_tokens


def test_a_result_loses_every_link_into_a_platform() -> None:
    result = {
        "status": "ok",
        "shape": "rows",
        "open_url": "https://opencti.internal/dashboard/x",
        "data": {"columns": [{"key": "name"}], "rows": [{"name": "APT29", "_url": "https://opencti.internal/x"}, "odd"]},
    }
    assert _without_links(result) == {
        "status": "ok",
        "shape": "rows",
        "open_url": None,
        "data": {"columns": [{"key": "name"}], "rows": [{"name": "APT29"}, "odd"]},
    }


@pytest.mark.parametrize(
    ("shape", "data", "expected"),
    [
        (
            "distribution",
            {"items": [{"label": "APT29", "value": 3.0, "entityType": "Intrusion-Set", "link": "https://opencti.internal/dashboard/id/1"}]},
            {"items": [{"label": "APT29", "value": 3.0, "entityType": "Intrusion-Set"}]},
        ),
        (
            "events",
            {"events": [{"label": "Report", "start": "2026-10-01", "link": "https://opencti.internal/r/1", "secondary": "x"}]},
            {"events": [{"label": "Report", "start": "2026-10-01", "secondary": "x"}]},
        ),
        (
            "outcomes",
            {
                "items": [{"label": "T1059", "success": 1.0, "id": "t1", "links": ["t2"], "link": "https://aev.internal/admin/simulations/s"}],
                "actions": [{"label": "Open", "link": "https://aev.internal/admin/findings"}],
            },
            {"items": [{"label": "T1059", "success": 1.0, "id": "t1", "links": ["t2"]}], "actions": [{"label": "Open"}]},
        ),
    ],
    ids=["distribution", "events", "outcomes"],
)
def test_a_result_loses_the_links_of_its_items_events_outcomes_and_actions(shape, data, expected) -> None:
    result = {"status": "ok", "shape": shape, "open_url": "https://opencti.internal/x", "data": data}
    assert _without_links(result) == {"status": "ok", "shape": shape, "open_url": None, "data": expected}


def test_the_snapshot_reads_with_the_pages_default_tokens() -> None:
    definition = {
        "defaults": {"tokens": {"default": {"sev": {"value": "Low"}, "bad name": {"value": "x"}, "n": {"value": 5}}}},
        "inputs": {
            "a": {"token": "sev", "defaultValue": "High"},
            "b": {"token": "hosts", "defaultValue": ["a", "b"]},
            "c": {"token": "flag", "defaultValue": True},
            "d": {"type": "input.button"},
        },
    }
    assert default_tokens(definition) == {"sev": "High", "n": "5", "hosts": ["a", "b"]}


def test_a_question_is_recognised_through_a_chain() -> None:
    sources = {
        "q": {"type": "ask"},
        "top": {"type": "derived", "options": {"extends": "q"}},
        "top2": {"type": "derived", "options": {"extends": "top"}},
        "plain": {"type": "static"},
        "loop": {"type": "derived", "options": {"extends": "loop"}},
    }
    definition = {"dataSources": sources}
    assert [asks_an_agent(definition, sid) for sid in ("q", "top", "top2", "plain", "missing")] == [True, True, True, False, False]
    # A chain longer than any the definition allows is treated as reading an agent: refused, never followed forever.
    assert asks_an_agent(definition, "loop") is True


def test_a_visitor_receives_what_draws_and_nothing_else() -> None:
    definition = {
        "version": 2,
        "title": "Ops",
        "dataSources": {"p": {"type": "platform", "name": "Reports", "options": {"integration_id": "secret-id"}}},
        "visualizations": {
            "v": {"type": "xtm.singlevalue", "dataSources": {"primary": "p"}, "options": {"link": {"url": "/x"}, "unit": "%"}, "eventHandlers": [{}]},
        },
        "inputs": {"i": {"type": "input.text", "token": "t"}},
        "layout": {
            "globalInputs": ["i"],
            "layoutDefinitions": {"l": {"type": "absolute", "structure": [{"item": "v"}, {"item": "i", "type": "input"}, {"item": "line", "type": "line"}]}},
        },
        "expressions": {"tokens": {"x": "$t$"}, "conditions": {"c": {"value": "isSet($t$)"}}},
    }
    snapshot = {"sources": {"p": {"status": "ok", "shape": "number"}}}
    assert public_definition(definition, snapshot) == {
        "version": 2,
        "title": "Ops",
        "dataSources": {"p": {"type": "static", "name": "Reports", "options": {"shape": "number", "data": {}}}},
        "visualizations": {"v": {"type": "xtm.singlevalue", "dataSources": {"primary": "p"}, "options": {"unit": "%"}}},
        "inputs": {},
        "layout": {"globalInputs": [], "layoutDefinitions": {"l": {"type": "absolute", "structure": [{"item": "v"}, {"item": "line", "type": "line"}]}}},
        "expressions": {"conditions": {"c": {"value": "isSet($t$)"}}},
    }
