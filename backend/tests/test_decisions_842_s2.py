# SPDX-License-Identifier: Apache-2.0

"""Human gates S2 (#842): the decisions dual write rules, the I.7 links and the router.

The transitions are pure functions of the hook (``decision_hooks``), tested
without a database; the dual write itself, the downgrade and the router e2e
run against PostgreSQL in ``test_decisions_842_s2_postgres.py``.
"""

import re
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models import decision_hooks as hooks
from app.models.decision import DECISION_KINDS, SOURCE_TYPES, public_id
from app.services import decision_links as links
from app.services import decision_router as router
from app.services.notification_feed import NOTIFICATION_KINDS

APP_TSX = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.tsx"
# Fixed, so pytest-xdist workers collect the same parametrize ids.
RID = uuid.UUID("7d2e47ff-e3b9-4f9d-bd3a-64aad67154f5")


# --- dual write rules ---------------------------------------------------------


def test_run_entering_awaiting_human_opens():
    assert hooks.run_transition("running", "awaiting_human", answered=False) == ("open",)


def test_run_answer_while_awaiting_answers_with_actor():
    out = hooks.run_transition("awaiting_human", "awaiting_human", answered=True, answer={"text": "B"}, actor="u")
    assert out == ("answer", {"text": "B"}, "u")


def test_run_leaving_unanswered_cancels():
    assert hooks.run_transition("awaiting_human", "cancelled", answered=False) == ("cancel", "run cancelled")


def test_run_leaving_answered_answers():
    assert hooks.run_transition("awaiting_human", "running", answered=True, answer={}, actor=None)[0] == "answer"


def test_run_other_changes_do_nothing():
    assert hooks.run_transition("running", "completed", answered=False) is None


def test_tool_approval_ask_has_three_options():
    kind, ask = hooks.run_ask("Approval required to run x", [], [{"tool_call_id": "c1"}])
    assert kind == "tool_approval"
    assert [o["key"] for o in ask["options"]] == ["approve", "approve_always", "reject"]


def test_hitl_ask_reads_the_paused_call_options():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "name": "request_human_input",
                    "arguments": {"question": "Merge now?", "options": [{"key": "A", "label": "Merge"}]},
                }
            ],
        }
    ]
    kind, ask = hooks.run_ask("Merge now?\n\nOptions: ...", messages, None)
    assert kind == "hitl"
    assert ask["question"] == "Merge now?"
    assert ask["options"][0]["key"] == "A"


def test_hitl_ask_falls_back_to_the_stored_question():
    assert hooks.run_ask("Which one?", None, None) == ("hitl", {"question": "Which one?"})


def test_flow_park_opens_answers_and_cancels():
    park = {"waiting": {"kind": "human", "node": "n1", "question": "Go?", "released": False}}
    assert hooks.flow_transition({}, park) == ("open",)
    assert hooks.flow_transition(park, park) is None
    released = {"waiting": {**park["waiting"], "released": True, "response": "yes", "responded_by": "a@b"}}
    assert hooks.flow_transition(park, released) == ("answer", {"text": "yes"}, "a@b")
    assert hooks.flow_transition(park, {"waiting": None}) == ("cancel", "flow run moved on")
    assert hooks.flow_transition({}, {"waiting": {"kind": "breakpoint"}}) is None


def test_work_item_needs_human_rules():
    assert hooks.work_item_transition(None, True, answer=None) == ("open",)
    assert hooks.work_item_transition(True, None, answer={"option": "A", "label": "Go", "answered_at": "x"}) == (
        "answer",
        {"option": "A", "label": "Go"},
        None,
    )
    assert hooks.work_item_transition(True, False, answer=None) == ("cancel", "no longer waiting for a person")
    assert hooks.work_item_transition(True, True, answer=None) is None


def test_recipient_rule_default():
    assert hooks.owner_recipient("abc") == "user:abc"
    assert hooks.owner_recipient("abc", company_managed=True) == "admins"
    assert hooks.owner_recipient(None, company="company") == "company"


def test_public_id_names_the_source():
    assert public_id("run", RID) == f"run:{RID}"
    assert set(SOURCE_TYPES) == {"run", "flow_run", "work_item"}
    assert set(DECISION_KINDS) == {"hitl", "tool_approval", "flow_ask", "work_item"}


# --- I.7: every link resolves -------------------------------------------------


def _frontend_routes() -> list[re.Pattern[str]]:
    source = APP_TSX.read_text(encoding="utf-8")
    patterns = []
    for path in re.findall(r'<Route\s+path="(/[^"]*)"', source):
        regex = re.sub(r"/:[A-Za-z]+\?", r"(?:/[^/?]+)?", path)
        regex = re.sub(r":[A-Za-z]+", r"[^/?]+", regex)
        patterns.append(re.compile(f"^{regex}$"))
    return patterns


def _resolves(route: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.match(route.split("?", 1)[0]) for p in patterns)


@pytest.mark.skipif(not APP_TSX.exists(), reason="frontend not in this checkout")
def test_every_decision_link_matches_a_frontend_route():
    patterns = _frontend_routes()
    ids = [uuid.uuid4(), "not-a-uuid", None]
    emitted = [links.work_item_link(i) for i in ids]
    emitted += [links.run_link(i) for i in ids]
    emitted += [links.flow_run_link(f, r) for f in ids for r in ids]
    emitted += [links.decision_link(s, uuid.uuid4(), flow_id=uuid.uuid4()) for s in SOURCE_TYPES]
    missing = [r for r in emitted if not _resolves(r, patterns)]
    assert not missing, f"links that open no page: {missing}"


@pytest.mark.skipif(not APP_TSX.exists(), reason="frontend not in this checkout")
def test_every_bell_kind_route_matches_a_frontend_route():
    """The feed builds each waiting-kind route through decision_links (I.7)."""
    from app.services import notification_feed as feed

    patterns = _frontend_routes()
    source = Path(feed.__file__).read_text(encoding="utf-8")
    assert "/operations/executions/{" not in source and "/tasks/work-items/{" not in source
    assert "/flows/library/{" not in source
    waiting = {"hitl", "flow_hitl", "work_item_waiting", "run_action_request"}
    assert waiting <= set(NOTIFICATION_KINDS)
    for route in (links.run_link(RID), links.work_item_link(RID), links.flow_run_link(RID, RID)):
        assert _resolves(route, patterns), route


def test_no_link_builds_the_removed_operations_work_items_route():
    for value in (RID, "x", None):
        assert "/operations/work-items" not in links.work_item_link(value)


def test_unknown_source_is_refused():
    with pytest.raises(ValueError):
        links.decision_link("chat", RID)


# --- router ---------------------------------------------------------------------


def test_parse_decision_id_forms():
    assert router.parse_decision_id(f"run:{RID}") == router.DecisionRef("run", RID)
    assert router.parse_decision_id(f"work_item:{RID}").source_type == "work_item"
    assert router.parse_decision_id(str(RID)) == RID


@pytest.mark.parametrize("raw", ["chat:" + str(RID), "run:nope", "", "garbage"])
def test_parse_decision_id_refuses(raw):
    with pytest.raises(HTTPException) as exc:
        router.parse_decision_id(raw)
    assert exc.value.status_code == 404


def test_run_response_option_and_text():
    ask = {"question": "?", "options": [{"key": "A", "label": "Merge"}, {"key": "B", "label": "Wait"}]}
    assert router.run_response(ask, "a", None) == "A"
    assert router.run_response(ask, "B", "after CI") == "B: after CI"
    assert router.run_response(ask, None, " free words ") == "free words"
    with pytest.raises(HTTPException) as exc:
        router.run_response(ask, "Z", None)
    assert exc.value.status_code == 400


async def test_empty_answer_is_refused_before_any_read():
    with pytest.raises(HTTPException) as exc:
        await router.answer_decision(None, f"run:{RID}", object(), option=None, text="  ")
    assert exc.value.status_code == 400


async def test_refusal_is_audited_on_the_decision_row(monkeypatch):
    class _Row:
        id = uuid.uuid4()
        ask = {}

    added, committed = [], []

    class _Db:
        async def rollback(self):
            return None

        def add(self, obj):
            added.append(obj)

        async def commit(self):
            committed.append(True)

    async def _resolve(db, raw):
        return router.DecisionRef("work_item", RID), _Row()

    async def _dispatch(*args, **kwargs):
        raise HTTPException(status_code=403, detail="not yours")

    monkeypatch.setattr(router, "resolve", _resolve)
    monkeypatch.setattr(router, "_dispatch", _dispatch)
    user = type("U", (), {"id": uuid.uuid4()})()
    with pytest.raises(HTTPException):
        await router.answer_decision(_Db(), f"work_item:{RID}", user, option="A", surface="bell")
    assert committed and added[0].event == "refused"
    assert added[0].surface == "bell" and added[0].detail["status"] == 403


async def test_unknown_surface_is_recorded_as_router(monkeypatch):
    seen = {}

    async def _resolve(db, raw):
        return router.DecisionRef("flow_run", RID), None

    async def _dispatch(db, ref, row, user, **kwargs):
        seen["surface"] = kwargs["surface"]
        seen["contextvar"] = hooks.answer_surface.get()
        return {"source_type": "flow_run", "source_id": str(RID), "status": "running"}

    monkeypatch.setattr(router, "resolve", _resolve)
    monkeypatch.setattr(router, "_dispatch", _dispatch)
    out = await router.answer_decision(None, f"flow_run:{RID}", object(), text="ok", surface="carrier-pigeon")
    assert seen == {"surface": "router", "contextvar": "router"}
    assert out["id"] == f"flow_run:{RID}"
    assert hooks.answer_surface.get() is None
