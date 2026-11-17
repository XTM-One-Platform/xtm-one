"""Programs computed live from work items and pull requests (#10833)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import work_item_programs as programs
from app.services.work_item_programs import (
    compute_program_status,
    is_program_root,
    linked_pull_requests,
    program_key,
    program_meta,
    with_program_key,
)

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)


def _item(id_: str, **fields) -> dict:
    base = {
        "id": id_,
        "title": f"item {id_}",
        "status": "open",
        "correlation_key": None,
        "payload": {},
        "blocked_on": [],
        "blocked_on_refs": [],
        "needs_human": False,
        "ask": None,
        "due_at": None,
        "owner_agent_id": "agent-1",
        "created_at": "2026-11-10T10:00:00+00:00",
    }
    base.update(fields)
    return base


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("program:ootb-routing", "program:ootb-routing"),
        ("ootb-routing", "program:ootb-routing"),
        ("  program:x  ", "program:x"),
        ("program:x/child", None),
        ("program:", None),
        ("", None),
    ],
)
def test_program_key(value, expected):
    assert program_key(value) == expected


def test_with_program_key_sets_and_clears_keeping_the_rest():
    payload = {"pull_requests": [{"repo": "o/r", "number": 1}], "program_key": "program:old"}
    linked = with_program_key(payload, "program:new")
    assert linked == {"pull_requests": [{"repo": "o/r", "number": 1}], "program_key": "program:new"}
    assert payload["program_key"] == "program:old"  # a copy, the stored payload untouched
    assert with_program_key(linked, None) == {"pull_requests": [{"repo": "o/r", "number": 1}]}
    assert with_program_key(None, "") == {}
    assert with_program_key(None, "program:x") == {"program_key": "program:x"}


def test_a_linked_item_of_any_key_is_a_child_with_its_blockers():
    # An item keyed for its owner's inbox (dev:...) joins a program by its payload alone.
    root = _item("root", correlation_key="program:x")
    child = _item(
        "c1",
        correlation_key="dev:x-slice",
        status="blocked",
        blocked_reason="waits for the review",
        payload=with_program_key({}, program_key("x")),
        blocked_on_refs=[{"kind": "pull_request", "repo": "o/r", "number": 7}],
    )
    assert child["correlation_key"] == "dev:x-slice"
    assert child["payload"]["program_key"] == "program:x"
    assert ("o/r", 7) in {(p["repo"], p["number"]) for p in linked_pull_requests([root, child])}
    status = compute_program_status(root, [child], [], now=NOW)
    assert status["children_by_status"].get("blocked") == 1


async def test_update_refuses_a_program_key_that_names_no_root_before_any_read():
    from app.services.work_item import update_work_item

    with pytest.raises(ValueError, match="not a program key"):
        await update_work_item(
            "00000000-0000-0000-0000-000000000001",
            user_id=None,
            program_key="program:x/slice-1",
            writer=None,  # never reached: refused before the session opens
        )


def test_is_program_root_only_for_the_root_key():
    assert is_program_root(_item("a", correlation_key="program:x"))
    assert not is_program_root(_item("b", correlation_key="program:x/slice-1"))
    assert not is_program_root(_item("c", correlation_key="dev:x"))
    assert not is_program_root(_item("d"))


def test_program_meta_falls_back_on_the_item_columns():
    root = _item(
        "r",
        correlation_key="program:x",
        due_at="2026-12-01T00:00:00+00:00",
        payload={"program": {"exit_criterion": "all slices merged", "unknown": "dropped", "owner": " "}},
    )
    assert program_meta(root) == {
        "exit_criterion": "all slices merged",
        "owner": "agent-1",
        "target_date": "2026-12-01T00:00:00+00:00",
    }


def test_linked_pull_requests_dedupes_payload_and_refs():
    a = _item("a", payload={"pull_requests": [{"repo": "Org/Repo", "number": 12}]})
    b = _item(
        "b",
        blocked_on_refs=[
            {"repo": "org/repo", "kind": "pull_request", "number": 12},
            {"repo": "org/repo", "kind": "deployment", "number": 13},
            {"repo": "org/repo", "kind": "issue", "number": 14},
            {"repo": "org/repo", "kind": "workflow_run", "workflow": "release.yml"},
        ],
    )
    assert linked_pull_requests([a, b]) == [
        {"repo": "org/repo", "number": 12, "item_ids": ["a", "b"]},
        {"repo": "org/repo", "number": 13, "item_ids": ["b"]},
    ]


def test_status_next_steps_owed_and_counts():
    root = _item("r", correlation_key="program:x")
    children = [
        _item("free", correlation_key="program:x/1"),
        _item("blocked", status="blocked", blocked_on=["free"]),
        _item("person", status="blocked", needs_human=True, due_at="2026-11-12T00:00:00+00:00"),
        _item("done", status="done", completed_at="2026-11-10T07:00:00+00:00"),
    ]
    status = compute_program_status(root, children, [], now=NOW)
    assert status["program"] == "program:x"
    assert status["children_by_status"] == {"open": 1, "blocked": 2, "done": 1}
    assert [n["id"] for n in status["next_steps"]] == ["free"]
    assert [o["id"] for o in status["owed_to_user"]] == ["person"]
    assert status["owed_to_user"][0]["age_hours"] == 4.0
    assert status["flags"] == []
    # Every line says when it was read and which object it came from.
    assert all(c["read_at"] == "2026-11-10T14:00:00Z" and c["id"] for c in status["children"])
    assert status["root"]["read_at"] == "2026-11-10T14:00:00Z"


def test_a_completed_child_changes_the_next_call():
    root = _item("r", correlation_key="program:x")
    child = _item("c")
    before = compute_program_status(root, [child], [], now=NOW)
    after = compute_program_status(root, [{**child, "status": "done"}], [], now=NOW)
    assert before["children_by_status"] == {"open": 1}
    assert after["children_by_status"] == {"done": 1}
    assert after["next_steps"] == []
    assert "every child is finished" in after["flags"][0]


def test_next_owed_to_user_without_a_dated_child_is_flagged():
    root = _item("r", correlation_key="program:x", payload={"program": {"next_owed_to_user": "demo to Sam"}})
    undated = _item("u", needs_human=True)
    flagged = compute_program_status(root, [undated], [], now=NOW)
    assert any("next_owed_to_user has no dated work item" in f for f in flagged["flags"])
    dated = {**undated, "due_at": "2026-11-11T09:00:00+00:00"}
    assert compute_program_status(root, [dated], [], now=NOW)["flags"] == []


def test_ask_counts_as_owed_to_the_user():
    root = _item("r", correlation_key="program:x")
    child = _item("a", ask={"question": "ship it?", "options": []})
    assert [o["id"] for o in compute_program_status(root, [child], [], now=NOW)["owed_to_user"]] == ["a"]


@pytest.mark.asyncio
async def test_read_pull_requests_reads_closings_then_open_ci(monkeypatch):
    from app.integrations.pull_request_events import PullRequestEvent
    from app.services import work_item_pull_requests as wipr

    async def closings(entries, user_id):
        return [
            PullRequestEvent(
                kind="closed", repo="org/repo", pr_numbers=(1,), merged=True, merge_sha="abc123", closed_at="t"
            )
        ]

    seen: list = []

    async def live(entries, user_id):
        seen.extend((e["repo"], e["number"]) for e in entries)
        return {("org/repo", 2): {"head_sha": "deadbeef", "draft": True, "conclusion": None}}

    monkeypatch.setattr(wipr, "read_pull_request_closings", closings)
    monkeypatch.setattr(wipr, "read_live_ci", live)
    out = await programs.read_pull_requests(
        [
            {"repo": "org/repo", "number": 1, "item_ids": ["a"]},
            {"repo": "org/repo", "number": 2, "item_ids": ["b"]},
            {"repo": "org/repo", "number": 3, "item_ids": ["c"]},
        ],
        "user",
    )
    assert seen == [("org/repo", 2), ("org/repo", 3)]
    assert [(p["number"], p["state"]) for p in out] == [(1, "merged"), (2, "open"), (3, "unread")]
    assert out[0]["merge_sha"] == "abc123"
    assert out[1]["head_sha"] == "deadbeef" and out[1]["draft"] is True and out[1]["ci"] is None
    assert all(p["read_at"] for p in out)


@pytest.mark.asyncio
async def test_get_program_status_reads_nothing_but_items_and_prs(monkeypatch):
    root = _item("11111111-1111-1111-1111-111111111111", correlation_key="program:x")
    child = _item("c", payload={"pull_requests": [{"repo": "org/repo", "number": 5}]})
    calls: list[dict] = []

    async def fake_list(**kwargs):
        calls.append(kwargs)
        if kwargs.get("correlation_key") == "program:x":
            return [root]
        if kwargs.get("program_key") == "program:x":
            return [child]
        return []

    async def fake_prs(entries, user_id):
        return [{**e, "state": "merged", "read_at": "now"} for e in entries]

    import app.services.work_item as wi

    monkeypatch.setattr(wi, "list_work_items", fake_list)
    monkeypatch.setattr(programs, "read_pull_requests", fake_prs)
    status = await programs.get_program_status("x", user_id="u")
    assert status is not None
    assert [p["number"] for p in status["pull_requests"]] == [5]
    assert status["pull_requests"][0]["state"] == "merged"
    assert [c["id"] for c in status["children"]] == ["c"]
    assert await programs.get_program_status("program:x/child", user_id="u") is None
