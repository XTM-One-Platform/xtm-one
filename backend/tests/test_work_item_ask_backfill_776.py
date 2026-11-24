"""Existing decision cards rewritten for their reader, once per release (#776, slice 5)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import fleet_health_signals as signals
from app.services import work_item_ask_backfill as backfill

GOOD = {
    "question": "Post Legal's comment asking the supplier for the missing documents?",
    "context": "The contract review found 4 documents missing.\nGo posts the prepared comment.",
    "options": [{"key": "A", "label": "Post it"}, {"key": "B", "label": "Close the review"}],
    "recommendation": "A",
}


def _item(**kw: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "title": "Supplier contract review",
        "description": "Legal reviewed the contract.",
        "blocked_reason": "CARD SENT 2026-11-20T11:43:58Z (msg 1795175038574), timer legal:fin-796-reminder",
        "ask": None,
        "workspace_id": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _rewriter(answer: object, calls: list[tuple[str, str]] | None = None):
    async def rewrite(system: str, user: str) -> str | None:
        if calls is not None:
            calls.append((system, user))
        return answer if answer is None or isinstance(answer, str) else json.dumps(answer)

    return rewrite


# ── What a card a person cannot read is ─────────────────────────────────────


def test_a_complete_plain_card_is_readable() -> None:
    assert backfill.ask_readable(GOOD)
    assert not backfill.card_unreadable(GOOD)


@pytest.mark.parametrize(
    "ask",
    [
        None,
        {},
        {"question": "CARD SENT 2026-11-20T11:43:58Z: waiting for GO"},
        {**GOOD, "context": "Item 16fe84b0 waits."},
        {
            **GOOD,
            "options": [{"key": "A", "label": "Go", "effect": "replies to msg 1795175038574"}, GOOD["options"][1]],
        },
        {"question": GOOD["question"], "options": GOOD["options"]},  # no context
        {"question": GOOD["question"], "context": GOOD["context"], "options": GOOD["options"][:1]},
        # No recommendation: refuse_needs_human_without_ask would refuse it (#762).
        {k: v for k, v in GOOD.items() if k != "recommendation"},
    ],
)
def test_missing_incomplete_or_technical_cards_are_not_readable(ask: object) -> None:
    assert backfill.card_unreadable(ask)


# ── The rewrite of one item ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_legacy_item_gets_a_readable_ask_and_keeps_its_technical_text() -> None:
    item = _item()
    rewrite = await backfill.rewrite_item(item, _rewriter(f"```json\n{json.dumps(GOOD)}\n```"))
    assert rewrite is not None
    assert rewrite.ask["question"] == GOOD["question"]
    assert len(rewrite.ask["options"]) == 2 and rewrite.ask["recommendation"] == "A"
    assert not backfill.card_unreadable(rewrite.ask)
    # Nothing to keep: the item had no question; blocked_reason stays as it was.
    assert rewrite.blocked_reason is None


@pytest.mark.asyncio
async def test_an_unreadable_question_is_kept_under_technical_details_with_the_deadline() -> None:
    old = {"question": "CARD SENT 2026-11-20T11:43:58Z: waiting", "deadline": "2026-11-24T09:00:00+00:00"}
    rewrite = await backfill.rewrite_item(_item(ask=old), _rewriter(GOOD))
    assert rewrite is not None
    assert rewrite.ask["deadline"] == "2026-11-24T09:00:00+00:00"
    assert rewrite.blocked_reason is not None
    assert rewrite.blocked_reason.startswith("CARD SENT")
    assert rewrite.blocked_reason.endswith(f"{backfill.KEPT_QUESTION_PREFIX} {old['question']}")


@pytest.mark.asyncio
async def test_a_readable_structured_ask_is_kept_and_only_gets_its_context() -> None:
    structured = {k: v for k, v in GOOD.items() if k != "context"}
    calls: list[tuple[str, str]] = []
    rewrite = await backfill.rewrite_item(
        _item(ask=structured), _rewriter({"context": "Four documents are missing."}, calls)
    )
    assert rewrite is not None
    assert calls[0][0] == backfill.CONTEXT_PROMPT
    assert rewrite.ask == {**structured, "context": "Four documents are missing."}
    assert rewrite.blocked_reason is None


@pytest.mark.parametrize(
    "answer",
    [
        None,  # model unreachable
        "I cannot tell.",  # no JSON
        {},  # the model says the item does not say
        {**GOOD, "question": "Reply to msg 1795175038574?"},  # still technical: refused by the validator
        {"question": GOOD["question"], "context": GOOD["context"], "options": GOOD["options"][:1]},
        {k: v for k, v in GOOD.items() if k != "recommendation"},  # #762: no recommendation
    ],
)
@pytest.mark.asyncio
async def test_a_rewrite_that_is_not_confident_is_not_stored(answer: object) -> None:
    assert await backfill.rewrite_item(_item(), _rewriter(answer)) is None


def test_the_item_prompt_is_bounded() -> None:
    prompt = backfill.item_prompt(_item(description="x" * 50_000, blocked_reason="y" * 50_000))
    assert len(prompt) < 6_000


@pytest.mark.asyncio
async def test_a_backfilled_card_passes_the_needs_human_refusal() -> None:
    """#762 x #776: a card the pass stores keeps the item's needs_human writable."""
    from app.services.work_item_blocked_refs import refuse_needs_human_without_ask

    rewrite = await backfill.rewrite_item(_item(), _rewriter(GOOD))
    assert rewrite is not None
    refuse_needs_human_without_ask(True, rewrite.ask)


# ── The pass: selection, idempotence, report ─────────────────────────────────


class _Store:
    """The pass's three reads and writes on a list of items: the writes applied in place."""

    def __init__(self, items: list[SimpleNamespace], fleets: dict[uuid.UUID, str]) -> None:
        self.items = items
        self.fleets = fleets
        self.updates: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def waiting(db: Any) -> list[Any]:
            return list(self.items)

        async def fleets(db: Any) -> dict[uuid.UUID, str]:
            return dict(self.fleets)

        async def store(db: Any, item_id: Any, values: dict[str, Any]) -> bool:
            target = next(i for i in self.items if i.id == item_id)
            for key, value in values.items():
                setattr(target, key, value)
            self.updates.append({"id": str(item_id), **values})
            return True

        monkeypatch.setattr(backfill, "_waiting_items", waiting)
        monkeypatch.setattr(backfill, "_fleets_by_workspace", fleets)
        monkeypatch.setattr(backfill, "_store", store)


@pytest.mark.asyncio
async def test_the_pass_rewrites_reports_and_changes_nothing_the_second_time(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, fleet = uuid.uuid4(), uuid.uuid4()
    legacy = _item(workspace_id=workspace)
    readable = _item(ask=dict(GOOD), blocked_reason="ids live here")
    hopeless = _item(title="?", description=None, blocked_reason="msg 1795175038574", workspace_id=workspace)
    store = _Store([legacy, readable, hopeless], {workspace: str(fleet)})
    store.install(monkeypatch)

    async def rewrite(system: str, user: str) -> str | None:
        return "{}" if "Title: ?" in user else json.dumps(GOOD)

    report = await backfill.backfill_readable_asks(None, rewrite)
    assert report.rewritten == [str(legacy.id)]
    assert report.unrewritable == [(str(hopeless.id), str(fleet))]
    assert legacy.ask["question"] == GOOD["question"]
    assert readable.ask == GOOD  # a readable card is never read again
    assert [u["id"] for u in store.updates] == [str(legacy.id)]
    # No status, activity or owner is written: the ask (and a kept question) only.
    assert set(store.updates[0]) <= {"id", "ask", "blocked_reason"}

    calls: list[str] = []

    async def recorded(system: str, user: str) -> str | None:
        calls.append(user)
        return await rewrite(system, user)

    again = await backfill.backfill_readable_asks(None, recorded)
    assert again.rewritten == []
    assert again.unrewritable == [(str(hopeless.id), str(fleet))]
    assert [c for c in calls if "Supplier contract review" in c] == []  # the rewritten card is not read again
    assert len(store.updates) == 1


@pytest.mark.asyncio
async def test_the_pass_stops_at_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_item() for _ in range(3)]
    _Store(items, {}).install(monkeypatch)
    report = await backfill.backfill_readable_asks(None, _rewriter(GOOD), limit=2)
    assert report.rewritten == [str(items[0].id), str(items[1].id)]
    assert items[2].ask is None


@pytest.mark.asyncio
async def test_the_release_pass_skips_without_a_version() -> None:
    assert await backfill.on_release_deployed({}) is None


@pytest.mark.asyncio
async def test_the_release_pass_does_nothing_while_agentic_ai_may_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import agentic_gate

    async def refused(db: Any) -> str:
        return agentic_gate.AI_DISABLED_CODE

    async def never(system: str, user: str) -> str | None:
        raise AssertionError("no model call with agentic AI refused")

    monkeypatch.setattr(agentic_gate, "agentic_refusal", refused)
    assert await backfill.on_release_deployed({"version": "1.0.0", "git_sha": "abc"}, rewriter=never) is None


@pytest.mark.asyncio
async def test_a_refused_boot_pass_takes_no_lease_and_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """#806: the boot call ran before agentic AI was allowed and the build never got its pass."""
    from app.services import agentic_gate

    release = {"version": "9.9.9-806", "git_sha": "refused"}
    taken: list[str] = []
    state: dict[str, str | None] = {"refusal": agentic_gate.LICENSE_REQUIRED_CODE}

    async def refusal(db: Any) -> str | None:
        return state["refusal"]

    async def held(rel: dict[str, Any]) -> bool:
        return False

    async def take(rel: dict[str, Any]) -> bool:
        taken.append(rel["version"])
        return True

    async def empty(db: Any, rewriter: Any) -> backfill.BackfillReport:
        return backfill.BackfillReport()

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    from app.core import database

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", refusal)
    monkeypatch.setattr(backfill, "_lease_held", held)
    monkeypatch.setattr(backfill, "_take_lease", take)
    monkeypatch.setattr(backfill, "backfill_readable_asks", empty)
    monkeypatch.setattr(backfill, "_done", set())

    assert await backfill.on_release_deployed(release, rewriter=_rewriter(GOOD)) is None
    assert taken == []  # the refusal leaves the lease for the next try
    assert backfill._lease_key(release) not in backfill._done

    state["refusal"] = None
    report = await backfill.on_release_deployed(release, rewriter=_rewriter(GOOD))
    assert report is not None and taken == [release["version"]]
    assert backfill._lease_key(release) in backfill._done

    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(backfill, "schedule_readable_ask_backfill", scheduled.append)
    backfill.schedule_if_due(release)
    assert scheduled == []  # ran once in this process: the tick asks no more


def test_the_tick_asks_again_for_a_build_not_yet_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[dict[str, Any]] = []
    monkeypatch.setattr(backfill, "schedule_readable_ask_backfill", scheduled.append)
    monkeypatch.setattr(backfill, "_done", set())
    backfill.schedule_if_due({})
    backfill.schedule_if_due({"version": "9.9.9-806", "git_sha": None})
    assert scheduled == [{"version": "9.9.9-806", "git_sha": None}]


def test_the_build_tick_retries_the_pass() -> None:
    from pathlib import Path

    source = Path(backfill.__file__).with_name("platform_builds.py").read_text(encoding="utf-8")
    assert "schedule_if_due(" in source


def test_the_release_hook_schedules_the_pass() -> None:
    from pathlib import Path

    source = Path(backfill.__file__).with_name("platform_builds.py").read_text(encoding="utf-8")
    assert "schedule_readable_ask_backfill(deployed)" in source


# ── The fleet health finding ─────────────────────────────────────────────────


def test_fleet_health_knows_the_unreadable_card_finding() -> None:
    assert signals.FINDING_FIX["decision_card_not_readable"] == signals.FIX_OWNER
    assert "blocked_reason" in signals.FINDING_ADVICE["decision_card_not_readable"]
