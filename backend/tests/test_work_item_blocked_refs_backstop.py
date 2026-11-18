"""The backstop of the blocked references and the note of a reference that cannot be read.

``release_satisfied_blocks`` releases a blocked item whose references are all met
but that stayed blocked (whatever path missed the release); ``note_unread_references``
tells an item, at most once per window, why the poller could not read a reference.
Unit level, with a session double.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import work_item_blocked_refs as refs_mod

NOW = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)
MERGED = {"repo": "o/r", "kind": "pull_request", "number": 1, "release_on": "merged", "state": "merged"}
RAN = {
    "repo": "o/release",
    "kind": "workflow_run",
    "workflow": "release.yml",
    "after": "2026-11-18T19:48:44Z",
    "release_on": "succeeded",
    "state": "succeeded",
}
OPEN_PR = {"repo": "o/r", "kind": "pull_request", "number": 2, "release_on": "merged"}


def _item(refs: list, **extra: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "status": "blocked",
        "blocked_on": None,
        "blocked_on_refs": refs,
        "blocked_reason": "waits",
        "needs_human": None,
        "claim_notes": [],
        "description": "brief",
        "not_before": None,
        "claimed_by_agent_id": uuid.uuid4(),
        "claimed_by_run_id": uuid.uuid4(),
        "claimed_at": NOW,
        "last_activity_at": NOW - timedelta(hours=2),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


class _Scalars:
    def __init__(self, items: list) -> None:
        self._items = items

    def all(self) -> list:
        return self._items


class _Result:
    def __init__(self, items: list) -> None:
        self._items = items

    def scalars(self) -> _Scalars:
        return _Scalars(self._items)

    def scalar_one_or_none(self) -> Any:
        return self._items[0] if self._items else None


class _Session:
    def __init__(self, store: dict) -> None:
        self.store = store

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, statement: Any) -> _Result:
        return _Result(list(self.store["items"]))

    async def commit(self) -> None:
        self.store["commits"] += 1


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict:
    data: dict = {"items": [], "commits": 0, "published": []}

    async def published(item_id: uuid.UUID, note: str) -> None:
        data["published"].append((item_id, note))

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(data))
    monkeypatch.setattr(refs_mod, "_publish_surfaced", published)
    return data


# -- the backstop ------------------------------------------------------------------


async def test_an_item_with_every_reference_met_is_released(store: dict) -> None:
    item = _item([MERGED, RAN])
    store["items"] = [item]
    assert await refs_mod.release_satisfied_blocks(NOW) == 1
    assert item.status == "open" and item.blocked_on_refs is None and item.blocked_reason is None
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None and item.last_activity_at == NOW
    assert item.description.startswith("released by the backstop: every reference was already met (")
    assert item.claim_notes[-1]["text"] == item.description.split("\n\n", 1)[0]
    assert [i for i, _ in store["published"]] == [item.id]


@pytest.mark.parametrize(
    "item",
    [
        _item([MERGED, OPEN_PR]),
        _item([MERGED], blocked_on=[str(uuid.uuid4())]),
        _item([MERGED], needs_human=True),
        _item([]),
    ],
    ids=["one-reference-open", "waits-for-a-work-item", "waits-for-a-person", "no-reference"],
)
async def test_an_item_with_something_left_to_wait_for_stays_blocked(store: dict, item: SimpleNamespace) -> None:
    store["items"] = [item]
    assert await refs_mod.release_satisfied_blocks(NOW) == 0
    assert item.status == "blocked" and store["published"] == []


async def test_the_backstop_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.database as database

    def broken() -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(database, "async_session", broken)
    assert await refs_mod.release_satisfied_blocks(NOW) == 0


# -- the read-failure note ---------------------------------------------------------


def test_the_note_names_each_failure_once() -> None:
    note = refs_mod.read_failure_note(["pr o/r#2: 401", "pr o/r#2: 401", "run: 404"], NOW)
    assert note.startswith(refs_mod.READ_FAILURE_NOTE_PREFIX)
    assert "(pr o/r#2: 401; run: 404)" in note and note.endswith("every 30 min")


def test_the_note_waits_for_the_item_to_be_blocked_a_while() -> None:
    assert not refs_mod.read_failure_note_due([], NOW - timedelta(minutes=5), NOW)
    assert refs_mod.read_failure_note_due([], NOW - timedelta(minutes=16), NOW)


def test_the_note_is_written_once_per_window() -> None:
    old = NOW - timedelta(minutes=30)
    recent = [{"at": (NOW - timedelta(hours=1)).isoformat(), "text": f"{refs_mod.READ_FAILURE_NOTE_PREFIX} x"}]
    stale = [{"at": (NOW - timedelta(hours=7)).isoformat(), "text": f"{refs_mod.READ_FAILURE_NOTE_PREFIX} x"}]
    other = [{"at": NOW.isoformat(), "text": "started"}]
    assert not refs_mod.read_failure_note_due(recent, old, NOW)
    assert refs_mod.read_failure_note_due(stale, old, NOW)
    assert refs_mod.read_failure_note_due(other, old, NOW)


async def test_an_unread_reference_is_noted_on_its_blocked_item(store: dict) -> None:
    item = _item([OPEN_PR])
    store["items"] = [item]
    before = item.last_activity_at
    assert await refs_mod.note_unread_references({item.id: ["pr o/r#2: 401 bad credentials"]}, NOW) == 1
    assert item.claim_notes[-1]["text"].startswith(refs_mod.READ_FAILURE_NOTE_PREFIX)
    assert "401 bad credentials" in item.claim_notes[-1]["text"]
    assert item.status == "blocked" and item.last_activity_at == before and item.description == "brief"
    # A second pass inside the window writes nothing.
    assert await refs_mod.note_unread_references({item.id: ["pr o/r#2: 401 bad credentials"]}, NOW) == 0


async def test_an_item_no_longer_blocked_gets_no_note(store: dict) -> None:
    item = _item([OPEN_PR], status="open")
    store["items"] = [item]
    assert await refs_mod.note_unread_references({item.id: ["x: 404"]}, NOW) == 0
    assert item.claim_notes == []
