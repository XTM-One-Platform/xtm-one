"""LLM usage by user and by group (``services/tracing/usage_breakdown.py``).

The per-user rows come from ``TraceReadStore.analytics_usage_by_user``; these
tests pin how the two admin views fold them: who is listed, what a user with no
account or no group becomes, and how a user in several groups is counted.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.tracing import usage_breakdown


class _Session:
    """Answers the three lookups from in-memory tables, filtered by the ids asked."""

    def __init__(self, *, accounts=None, memberships=None, groups=None):
        self.accounts = accounts or {}
        self.memberships = memberships or {}
        self.groups = groups or {}
        self.queries: list[tuple[str, list]] = []

    async def execute(self, statement):
        sql = str(statement)
        asked = next(
            (value for value in statement.compile().params.values() if isinstance(value, list | tuple)),
            [],
        )
        if "FROM groups" in sql:
            self.queries.append(("groups", list(asked)))
            rows = [(group_id, *self.groups[group_id]) for group_id in asked if group_id in self.groups]
        elif "FROM group_members" in sql:
            self.queries.append(("memberships", list(asked)))
            rows = [
                (user_id, group_id)
                for user_id in asked
                for group_id in self.memberships.get(user_id, ())
            ]
        elif "FROM users" in sql:
            self.queries.append(("accounts", list(asked)))
            rows = [(user_id, *self.accounts[user_id]) for user_id in asked if user_id in self.accounts]
        else:
            raise AssertionError(f"Unexpected query: {sql}")
        return SimpleNamespace(all=lambda: rows)

    def asked(self, kind: str) -> list:
        return [ids for name, ids in self.queries if name == kind]


def _row(user_id, *, traces: int, cost: float, tokens: int = 100, agents: int = 1) -> dict:
    return {
        "user_id": None if user_id is None else str(user_id),
        "trace_count": traces,
        "total_tokens": tokens,
        "total_cost": cost,
        "agent_count": agents,
    }


ALICE, BOB, CAROL, DAVE = (uuid.uuid4() for _ in range(4))
ALPHA, BETA, GAMMA = (uuid.uuid4() for _ in range(3))


def _platform() -> _Session:
    return _Session(
        accounts={
            ALICE: ("alice@example.com", "Alice", True),
            BOB: ("bob@example.com", None, False),
            DAVE: ("dave@example.com", "Dave", True),
        },
        memberships={ALICE: [BETA, ALPHA], BOB: [ALPHA]},
        groups={ALPHA: ("alpha", 3), BETA: ("Beta", 1), GAMMA: ("Gamma", 5)},
    )


def _rows() -> list[dict]:
    return [
        _row(BOB, traces=2, cost=1.0, tokens=200),
        _row(ALICE, traces=4, cost=3.0, tokens=800, agents=2),
        _row(CAROL, traces=1, cost=0.5, tokens=50),
        _row(DAVE, traces=0, cost=0.0, tokens=0, agents=0),
        _row(None, traces=3, cost=0.25, tokens=30),
    ]


@pytest.mark.asyncio
async def test_users_are_listed_costliest_first_with_who_they_are():
    db = _platform()

    payload = await usage_breakdown.usage_by_user(db, _rows(), limit=10)

    assert [item["user_id"] for item in payload["items"]] == [str(ALICE), str(BOB), str(CAROL)]
    alice, bob, carol = payload["items"]
    assert alice == {
        "user_id": str(ALICE),
        "user_email": "alice@example.com",
        "user_name": "Alice",
        "is_active": True,
        "group_names": ["alpha", "Beta"],
        "agent_count": 2,
        "trace_count": 4,
        "total_tokens": 800,
        "total_cost": 3.0,
        "avg_cost_per_trace": 0.75,
    }
    assert (bob["user_name"], bob["is_active"], bob["group_names"]) == (None, False, ["alpha"])
    assert (carol["user_email"], carol["is_active"], carol["group_names"]) == (None, None, [])
    assert payload["total_users"] == 3
    assert payload["other_users"] is None
    assert payload["unattributed"] == {
        "trace_count": 3,
        "total_tokens": 30,
        "total_cost": 0.25,
        "avg_cost_per_trace": pytest.approx(0.25 / 3),
    }
    listed = sum(item["total_cost"] for item in payload["items"]) + payload["unattributed"]["total_cost"]
    assert listed == pytest.approx(sum(row["total_cost"] for row in _rows()))


@pytest.mark.asyncio
async def test_the_user_limit_keeps_the_total_and_looks_up_only_the_users_shown():
    db = _platform()

    payload = await usage_breakdown.usage_by_user(db, _rows(), limit=1)

    assert [item["user_id"] for item in payload["items"]] == [str(ALICE)]
    assert payload["total_users"] == 3
    assert payload["other_users"] == {
        "user_count": 2,
        "trace_count": 3,
        "total_tokens": 250,
        "total_cost": 1.5,
        "avg_cost_per_trace": 0.5,
    }
    listed = [*payload["items"], payload["other_users"], payload["unattributed"]]
    assert sum(part["total_cost"] for part in listed) == pytest.approx(sum(row["total_cost"] for row in _rows()))
    assert sum(part["trace_count"] for part in listed) == sum(row["trace_count"] for row in _rows())
    assert db.asked("accounts") == [[ALICE]]
    assert db.asked("memberships") == [[ALICE]]
    assert [set(ids) for ids in db.asked("groups")] == [{ALPHA, BETA}]


@pytest.mark.asyncio
async def test_users_of_equal_cost_rank_by_traces_then_id():
    first, second = sorted((uuid.uuid4(), uuid.uuid4()), key=str)
    rows = [
        _row(second, traces=1, cost=1.0),
        _row(first, traces=1, cost=1.0),
        _row(uuid.uuid4(), traces=5, cost=1.0),
    ]

    payload = await usage_breakdown.usage_by_user(_Session(), rows, limit=10)

    assert [item["user_id"] for item in payload["items"]][1:] == [str(first), str(second)]
    assert payload["items"][0]["trace_count"] == 5


@pytest.mark.asyncio
async def test_a_window_with_no_user_reads_no_group():
    db = _Session()

    payload = await usage_breakdown.usage_by_user(db, [_row(None, traces=2, cost=0.5)], limit=10)

    assert payload["items"] == []
    assert payload["total_users"] == 0
    assert payload["other_users"] is None
    assert payload["unattributed"]["trace_count"] == 2
    assert db.asked("groups") == []


@pytest.mark.asyncio
async def test_groups_count_each_member_and_say_when_memberships_overlap():
    db = _platform()

    payload = await usage_breakdown.usage_by_group(db, _rows())

    alpha, beta = payload["items"]
    assert alpha == {
        "group_id": str(ALPHA),
        "group_name": "alpha",
        "member_count": 3,
        "active_user_count": 2,
        "avg_cost_per_active_user": 2.0,
        "trace_count": 6,
        "total_tokens": 1000,
        "total_cost": 4.0,
        "avg_cost_per_trace": pytest.approx(4.0 / 6),
    }
    assert (beta["group_name"], beta["active_user_count"], beta["total_cost"]) == ("Beta", 1, 3.0)
    assert payload["overlapping_membership"] is True
    # GAMMA has members but none of them ran a trace: it is never read.
    assert [set(ids) for ids in db.asked("groups")] == [{ALPHA, BETA}]
    assert payload["ungrouped"] == {
        "active_user_count": 1,
        "avg_cost_per_active_user": 0.5,
        "trace_count": 1,
        "total_tokens": 50,
        "total_cost": 0.5,
        "avg_cost_per_trace": 0.5,
    }
    assert payload["unattributed"]["total_cost"] == 0.25


@pytest.mark.asyncio
async def test_groups_of_equal_cost_rank_by_traces_then_name():
    db = _Session(
        memberships={ALICE: [ALPHA], BOB: [BETA], CAROL: [GAMMA]},
        groups={ALPHA: ("zeta", 1), BETA: ("Eta", 1), GAMMA: ("theta", 1)},
    )
    rows = [
        _row(ALICE, traces=1, cost=1.0),
        _row(BOB, traces=1, cost=1.0),
        _row(CAROL, traces=2, cost=1.0),
    ]

    payload = await usage_breakdown.usage_by_group(db, rows)

    assert [item["group_name"] for item in payload["items"]] == ["theta", "Eta", "zeta"]
    assert payload["overlapping_membership"] is False
    assert payload["ungrouped"] is None
    assert payload["unattributed"] is None


@pytest.mark.asyncio
async def test_groups_named_alike_rank_by_id():
    first, second = sorted((uuid.uuid4(), uuid.uuid4()), key=str)
    db = _Session(
        memberships={ALICE: [second], BOB: [first]},
        groups={first: ("sales", 1), second: ("Sales", 1)},
    )
    rows = [_row(ALICE, traces=1, cost=1.0), _row(BOB, traces=1, cost=1.0)]

    payload = await usage_breakdown.usage_by_group(db, rows)

    assert [item["group_id"] for item in payload["items"]] == [str(first), str(second)]


@pytest.mark.asyncio
async def test_without_any_membership_every_user_is_ungrouped():
    db = _Session(groups={ALPHA: ("alpha", 0)})

    payload = await usage_breakdown.usage_by_group(db, _rows())

    assert payload["items"] == []
    assert payload["ungrouped"]["active_user_count"] == 3
    assert payload["ungrouped"]["trace_count"] == 7
    assert payload["overlapping_membership"] is False
    assert db.asked("groups") == []


@pytest.mark.asyncio
async def test_lookups_are_chunked(monkeypatch):
    monkeypatch.setattr(usage_breakdown, "_LOOKUP_CHUNK", 2)
    users = [uuid.uuid4() for _ in range(5)]
    db = _Session(
        accounts={user: (f"{user}@example.com", None, True) for user in users},
        memberships={user: [ALPHA] for user in users},
        groups={ALPHA: ("alpha", 5)},
    )
    rows = [_row(user, traces=1, cost=float(index + 1)) for index, user in enumerate(users)]

    by_user = await usage_breakdown.usage_by_user(db, rows, limit=10)
    by_group = await usage_breakdown.usage_by_group(db, rows)

    assert [len(ids) for ids in db.asked("accounts")] == [2, 2, 1]
    assert all(item["user_email"] for item in by_user["items"])
    assert all(item["group_names"] == ["alpha"] for item in by_user["items"])
    assert by_group["items"][0]["active_user_count"] == 5
    assert by_group["items"][0]["total_cost"] == 15.0
