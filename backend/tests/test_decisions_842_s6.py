# SPDX-License-Identifier: Apache-2.0

"""Human gates S6 (#842): the flow Ask-user node's options, recommendation and deadline.

Pure functions only (no database): the node's ask (``flow_ask``), the option an
answer chose (``flow_option``), the decision a park opens and answers
(``decision_hooks``) and the response the router writes for a flow
(``run_response``). The park-and-resume path itself runs against PostgreSQL in
``test_flow_runner_postgres.py``.
"""

import pytest

from app.models import decision_hooks as hooks
from app.services import decision_router as router
from app.services.decision_ask import flow_ask, flow_option
from app.services.flow_catalog import get_node_kind

OPTIONS = [
    {"key": "A", "label": "Publish", "effect": "The post goes live now"},
    {"key": "B", "label": "Hold", "effect": "The post waits for Monday"},
]


def test_a_node_without_options_keeps_the_free_text_park() -> None:
    assert flow_ask("Publish the post?") is None
    assert flow_ask("Publish the post?", options="", recommendation="", deadline="") is None


def test_a_node_with_options_parks_the_normalised_ask() -> None:
    ask = flow_ask("Publish the post?", OPTIONS, "b", "2026-12-01T17:00:00Z")
    assert ask is not None
    assert ask["question"] == "Publish the post?"
    assert [o["key"] for o in ask["options"]] == ["A", "B"]
    assert ask["recommendation"] == "B"
    assert ask["deadline"] == "2026-12-01T17:00:00+00:00"


def test_options_given_as_json_text_are_read() -> None:
    ask = flow_ask("Publish the post?", '[{"key": "A", "label": "Publish"}]')
    assert ask is not None and ask["options"] == [{"key": "A", "label": "Publish"}]


@pytest.mark.parametrize(
    ("options", "recommendation", "deadline", "reason"),
    [
        ("[not json", None, None, "not valid JSON"),
        (OPTIONS, "C", None, "is not the key of one of its options"),
        (OPTIONS, None, "tomorrow", "ISO-8601"),
        ([{"label": "no key"}], None, None, "needs a key"),
    ],
)
def test_a_bad_ask_fails_the_node_with_its_reason(
    options: object, recommendation: str | None, deadline: str | None, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        flow_ask("Publish the post?", options, recommendation, deadline)


@pytest.mark.parametrize(
    ("response", "option"),
    [("A", "A"), ("a", "A"), ("Hold", "B"), ("B: after the review", "B"), ("Something else", ""), ("", "")],
)
def test_the_answer_names_its_option(response: str, option: str) -> None:
    assert flow_option({"question": "q", "options": OPTIONS}, response) == option


def test_a_free_text_park_never_names_an_option() -> None:
    assert flow_option(None, "A") == ""


def test_the_decision_carries_the_parked_ask_and_its_deadline() -> None:
    ask = flow_ask("Publish the post?", OPTIONS, "A", "2026-12-01T17:00:00Z")
    assert ask is not None
    state = {"waiting": {"kind": "human", "node": "n1", "question": "Publish the post?", "ask": ask}}

    class Run:
        user_id = "00000000-0000-0000-0000-000000000001"
        is_company_managed = False

    kind, opened, _recipient, deadline = hooks.flow_opening(Run(), state)
    assert kind == "flow_ask"
    assert opened["options"] == ask["options"] and opened["recommendation"] == "A"
    assert deadline is not None and deadline.isoformat() == "2026-12-01T17:00:00+00:00"


def test_a_free_text_decision_keeps_the_question_alone() -> None:
    state = {"waiting": {"kind": "human", "node": "n1", "question": "Go?"}}

    class Run:
        user_id = "00000000-0000-0000-0000-000000000001"
        is_company_managed = True

    kind, opened, _recipient, deadline = hooks.flow_opening(Run(), state)
    assert (kind, opened, deadline) == ("flow_ask", {"question": "Go?"}, None)


def test_an_option_answer_is_recorded_on_the_decision() -> None:
    park = {"waiting": {"kind": "human", "node": "n1", "question": "Go?", "released": False}}
    released = {
        "waiting": {**park["waiting"], "released": True, "response": "B: later", "option": "B", "responded_by": "a@b"}
    }
    assert hooks.flow_transition(park, released) == ("answer", {"text": "B: later", "option": "B"}, "a@b")


def test_a_released_park_offers_no_answerable_card() -> None:
    """UX round 1, B1: once the answer is taken, the run panel shows no card."""
    from app.api.v1.flow_runs import awaiting_human

    park = {"waiting": {"kind": "human", "node": "n1", "question": "Go?", "ask": {"question": "Go?"}}}
    assert awaiting_human(park)["question"] == "Go?"
    assert awaiting_human({"waiting": {**park["waiting"], "released": True}}) == {}
    assert awaiting_human({"waiting": {"kind": "breakpoint", "node": "n1"}}) == {}
    assert awaiting_human(None) == {}


def test_the_router_sends_a_flow_the_option_key_and_the_note() -> None:
    ask = {"question": "q", "options": OPTIONS}
    assert router.run_response(ask, "b", None) == "B"
    assert router.run_response(ask, "A", "  ship it ") == "A: ship it"
    assert router.run_response(ask, None, "my own words") == "my own words"


def test_the_node_declares_its_new_fields_and_the_option_output() -> None:
    kind = get_node_kind("action.ask_user")
    assert kind is not None
    names = [f.name for f in kind.config_fields]
    assert names[:2] == ["title", "question"]
    assert {"options", "recommendation", "deadline"} <= set(names)
    assert not any(f.required for f in kind.config_fields if f.name in {"options", "recommendation", "deadline"})
    assert "option" in [v.name for v in kind.output_variables]
