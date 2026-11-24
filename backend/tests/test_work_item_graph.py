"""The work items graph (``services/work_item_graph.py``, ``GET /work-items/graph``).

The seeded set of the acceptance (#968): a chain A waits B waits C, a fork
(D waits B too), a pull request reference, a deployment reference, a person
card, a program with children and a cancelled blocker outside the filter.
"""

import asyncio
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import work_items as work_items_api
from app.services import work_item_graph
from app.services.work_item_graph import build_graph, missing_blocker_ids


def _item(iid, *, status="open", blocked_on=(), refs=(), key=None, payload=None, needs_human=False, ask=None, **extra):
    return {
        "id": iid,
        "title": f"item {iid}",
        "status": status,
        "priority": "medium",
        "correlation_key": key,
        "payload": payload or {},
        "blocked_on": list(blocked_on),
        "blocked_on_refs": list(refs),
        "needs_human": needs_human,
        "ask": ask,
        "user_id": "u-1",
        **extra,
    }


PR_REF = {"repo": "Org/Repo", "kind": "pull_request", "number": 12, "release_on": "merged"}
DEPLOY_REF = {"repo": "Org/Repo", "kind": "deployment", "number": 12, "release_on": "deployed", "state": "deployed"}
ASK = {
    "question": "Ship it?",
    "options": [{"key": "A", "label": "Yes"}, {"key": "B", "label": "No"}],
    "recommendation": "A",
}


def _seeded():
    items = [
        _item("A", status="blocked", blocked_on=["B"]),
        _item("B", status="blocked", blocked_on=["C", "X"], refs=[PR_REF, DEPLOY_REF]),
        _item(
            "C",
            needs_human=True,
            status="blocked",
            ask=ASK,
            context={"person": {"id": "u-1", "name": "Sam", "is_you": True}},
        ),
        _item("D", status="blocked", blocked_on=["B"]),
        _item(
            "R",
            key="program:launch",
            payload={"pull_requests": [{"repo": "Org/Repo", "number": 12, "url": "https://example.test/pr/12"}]},
        ),
        _item("K1", key="program:launch/slice-1"),
        _item("K2", key="dev:slice-2", payload={"program_key": "program:launch"}),
    ]
    blockers = [_item("X", status="cancelled", blocked_on=["Z"])]
    return items, blockers


def _edges(graph, kind):
    return {(e["source"], e["target"]) for e in graph["edges"] if e["kind"] == kind}


def test_chain_and_fork_follow_blocked_on_from_waiter_to_blocker():
    items, blockers = _seeded()
    graph = build_graph(items, blockers)
    assert {("A", "B"), ("B", "C"), ("D", "B"), ("B", "X")} <= _edges(graph, "blocked_on")


def test_a_blocker_outside_the_filter_is_drawn_marked_and_not_expanded():
    items, blockers = _seeded()
    graph = build_graph(items, blockers)
    x = next(n for n in graph["nodes"] if n["id"] == "X")
    assert x["outside_filter"] is True and x["status"] == "cancelled"
    # Z is neither read nor visible: no edge points at nothing.
    assert all(e["target"] != "Z" for e in graph["edges"])


def test_references_are_one_node_each_with_state_and_link():
    items, blockers = _seeded()
    graph = build_graph(items, blockers)
    refs = {n["id"]: n for n in graph["nodes"] if n["kind"] == "ref"}
    pr = refs["ref:pull_request:org/repo#12"]
    deploy = refs["ref:deployment:org/repo#12"]
    assert pr["satisfied"] is False and pr["url"] == "https://example.test/pr/12"
    assert deploy["satisfied"] is True and deploy["label"] == "Org/Repo#12"
    assert {("B", pr["id"]), ("B", deploy["id"])} <= _edges(graph, "ref")


def test_a_person_card_is_a_person_node_and_the_item_carries_its_ask():
    items, blockers = _seeded()
    graph = build_graph(items, blockers)
    person = next(n for n in graph["nodes"] if n["kind"] == "person")
    assert person == {"id": "person:u-1", "kind": "person", "name": "Sam", "is_you": True}
    c = next(n for n in graph["nodes"] if n["id"] == "C")
    assert c["ask"]["question"] == "Ship it?" and [o["key"] for o in c["ask"]["options"]] == ["A", "B"]
    assert ("C", "person:u-1") in _edges(graph, "person")


def test_an_item_node_carries_what_the_waiting_for_me_filter_reads() -> None:
    asked = {"id": "C", "title": "c", "status": "blocked", "needs_human": True, "user_id": "u-1"}
    graph = build_graph([asked])
    c = next(n for n in graph["nodes"] if n["id"] == "C")
    assert c["user_id"] == "u-1" and c["needs_human"] is True and c["scheduled"] is False
    assert {"waits_for_user_id", "owner_agent_id", "due_at"} <= c.keys()


def test_program_children_hang_from_their_root_by_key_and_by_payload():
    items, blockers = _seeded()
    graph = build_graph(items, blockers)
    assert _edges(graph, "program") == {("R", "K1"), ("R", "K2")}
    assert next(n for n in graph["nodes"] if n["id"] == "R")["is_program_root"] is True


def test_missing_blocker_ids_lists_each_absent_blocker_once():
    items, _ = _seeded()
    assert missing_blocker_ids(items) == ["X"]


def test_read_graph_reads_three_times_whatever_the_size(monkeypatch):
    """One list, one read of the missing blockers, one context lookup: no N+1."""
    items, blockers = _seeded()
    calls: list[str] = []

    async def list_work_items(**kwargs):
        calls.append("list")
        assert kwargs["limit"] == work_item_graph.MAX_GRAPH_ITEMS and kwargs["open_only"] is True
        return items

    async def by_ids(ids, *, user_id):
        calls.append("blockers")
        assert ids == ["X"]
        return blockers

    async def describe(rows, *, viewer):
        calls.append("context")
        return rows

    from app.services import work_item as work_item_service
    from app.services import work_item_context

    monkeypatch.setattr(work_item_service, "list_work_items", list_work_items)
    monkeypatch.setattr(work_item_service, "list_visible_work_items_by_ids", by_ids)
    monkeypatch.setattr(work_item_context, "describe_work_items", describe)

    graph = asyncio.run(work_item_graph.read_graph(user_id="u-1", viewer=None, filters={"open_only": True}))
    assert calls == ["list", "blockers", "context"]
    assert graph["truncated"] is False


def test_route_forwards_the_list_filters_and_is_declared_before_the_item_route(monkeypatch):
    seen = {}

    async def read_graph(*, user_id, viewer, filters):
        seen.update(filters, user_id=user_id)
        return {"nodes": [], "edges": [], "truncated": False}

    monkeypatch.setattr(work_item_graph, "read_graph", read_graph)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    app = FastAPI()
    app.include_router(work_items_api.router, prefix="/work-items")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    response = TestClient(app).get("/work-items/graph", params={"open_only": "true", "search": "deploy", "vis": "mine"})
    assert response.status_code == 200
    assert seen["open_only"] is True and seen["search"] == "deploy" and seen["ownership"] == "mine"
    assert seen["user_id"] == str(user.id)
    paths = [r.path for r in work_items_api.router.routes]
    assert paths.index("/graph") < paths.index("/{work_item_id}")
