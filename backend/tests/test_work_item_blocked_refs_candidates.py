"""The candidates a closing or a polling pass reads are never hidden behind older items.

The candidate queries used to cap the blocked items naming a repository at the
oldest 50 (a delivery) or 100 (a polling pass) BEFORE matching them against the
event: with more blocked items on the same repository than that, a closing never
reached a newer item it named, and the polling pass, which skips the items read in
its quiet window, never got past the oldest ones. An item re-parked after a merge
queue removal waited about 20 h for its merge. Unit level, with a session double.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_blocked_refs as refs_mod

REPO = "xtm-one-platform/xtm-one"


def _ref(number: int, **extra: Any) -> dict[str, Any]:
    return {"repo": REPO, "kind": "pull_request", "number": number, "release_on": "merged_or_closed", **extra}


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Db:
    """Answers every row it holds, cut at the query's own limit."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.limits: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        limit = statement._limit_clause.value if statement._limit_clause is not None else None
        self.limits.append(limit)
        return _Rows(self.rows[:limit] if limit is not None else self.rows)


def test_an_event_concerns_a_reference_on_its_number_or_its_head_branch() -> None:
    closing = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(1092,), head_branch="feat/x")
    assert refs_mod.concerns_event([_ref(1092)], closing)
    assert refs_mod.concerns_event([_ref(1000, head_branch="feat/x")], closing)
    assert not refs_mod.concerns_event([_ref(7)], closing)
    assert not refs_mod.concerns_event([{**_ref(1092), "repo": "other/repo"}], closing)
    assert not refs_mod.concerns_event(None, closing)


async def test_a_closing_reaches_an_item_behind_more_than_fifty_older_ones() -> None:
    older = [(uuid.uuid4(), [_ref(n)]) for n in range(1, 121)]
    named = uuid.uuid4()
    db = _Db([*older, (named, [_ref(1092)])])
    closing = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(1092,), merged=True)

    ids = await refs_mod._candidate_ids(db, closing)

    assert ids == [named]
    assert db.limits == [refs_mod._CANDIDATE_SCAN]


async def test_an_unsigned_closing_reads_back_an_item_behind_older_ones() -> None:
    user = uuid.uuid4()
    older = [(uuid.uuid4(), user, [_ref(n)]) for n in range(1, 121)]
    named = uuid.uuid4()
    db = _Db([*older, (named, user, [_ref(1092)])])
    closing = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(1092,), merged=True)

    rows = await refs_mod._hint_candidates(db, closing)

    assert [row[0] for row in rows] == [named]


async def test_the_polling_pass_scans_past_the_oldest_items() -> None:
    rows = [(uuid.uuid4(), uuid.uuid4(), [_ref(n)]) for n in range(1, 151)]
    db = _Db(rows)

    out = await refs_mod._poll_candidates(db, datetime(2026, 11, 23, tzinfo=UTC))

    assert len(out) == 150
    assert db.limits == [refs_mod._CANDIDATE_SCAN]
