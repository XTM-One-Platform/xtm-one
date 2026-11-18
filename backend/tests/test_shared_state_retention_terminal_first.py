"""Terminal-first retention of a shared state entry (#12035).

A retention policy with ``archive_when`` rolls only the items it matches, the
oldest first; an open item is never moved, and the write says when open items
were kept past the cap. A policy without it keeps the roll by age and warns.
``keep_last_json`` on a protected list follows the same rule.
"""

import uuid

import pytest

import app.services.shared_state as shared_state_service
from app.services.shared_state import (
    apply_list_ops,
    retention_protect,
    retention_shape,
    retention_usage,
    roll_lists,
    value_size,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _Session, _state

TERMINAL = {"state": {"in": ["merged", "closed"]}}


@pytest.fixture
def limits(monkeypatch):
    def _set(hard: int, max_items: int = 0) -> None:
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_soft_limit_chars", 0)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_hard_limit_chars", hard)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_list_max_items", max_items)

    return _set


def _sessions(monkeypatch, *sessions):
    queue = list(sessions)
    monkeypatch.setattr("app.core.database.async_session", lambda: queue.pop(0))


def _gaps(states: list[str], width: int = 40) -> list[dict]:
    return [{"n": i, "state": s, "text": "x" * width} for i, s in enumerate(states)]


# ── the policy shape ───────────────────────────────────────────────────────


def test_archive_when_takes_a_match_or_a_list_of_matches():
    assert retention_shape({"keep_last": 3, "archive_when": TERMINAL})["archive_when"] == TERMINAL
    many = [{"state": "merged"}, {"state": "closed"}]
    assert retention_shape({"max_chars": 900, "archive_when": many})["archive_when"] == many
    for bad in ({}, [], "merged", [{}], [{"state": "merged"}, "closed"]):
        with pytest.raises(ValueError, match="archive_when"):
            retention_shape({"keep_last": 3, "archive_when": bad})


# ── roll_lists ─────────────────────────────────────────────────────────────


def test_a_mixed_list_at_the_cap_rolls_only_terminal_items_oldest_first(limits):
    limits(0)
    value = {"gaps": _gaps(["queued", "merged", "queued", "closed", "merged", "queued"])}
    held: dict[str, int] = {}
    rolled = roll_lists(value, {"keep_last": 4, "archive_when": TERMINAL}, {}, held)
    assert [g["n"] for g in rolled["gaps"]] == [1, 3]
    assert [g["n"] for g in value["gaps"]] == [0, 2, 4, 5]
    assert held == {}


def test_max_chars_rolls_terminal_items_first_and_keeps_every_open_one(limits):
    limits(0)
    value = {"gaps": _gaps(["queued"] * 4 + ["merged"] * 4)}
    cap = value_size({"gaps": value["gaps"][:6]})
    rolled = roll_lists(value, {"max_chars": cap, "archive_when": TERMINAL})
    assert all(g["state"] == "merged" for g in rolled["gaps"])
    assert [g["n"] for g in value["gaps"] if g["state"] == "queued"] == [0, 1, 2, 3]
    assert value_size(value) <= cap


def test_a_list_of_open_items_at_the_cap_moves_nothing_and_says_so(limits):
    limits(0)
    value = {"gaps": _gaps(["queued"] * 6)}
    before = list(value["gaps"])
    held: dict[str, int] = {}
    rolled = roll_lists(value, {"keep_last": 4, "archive_when": TERMINAL}, {}, held)
    assert rolled == {} and value["gaps"] == before
    assert held == {"gaps": 2}
    held = {}
    rolled = roll_lists(value, {"max_chars": 200, "archive_when": TERMINAL}, {}, held)
    assert rolled == {} and value["gaps"] == before
    assert held == {"gaps": 6}


def test_without_archive_when_the_roll_by_age_is_unchanged(limits):
    limits(0)
    value = {"gaps": _gaps(["queued", "merged", "queued", "closed", "queued"])}
    rolled = roll_lists(value, {"keep_last": 3})
    assert [g["n"] for g in rolled["gaps"]] == [0, 1]
    assert [g["n"] for g in value["gaps"]] == [2, 3, 4]


# ── keep_last_json on a protected list ──────────────────────────────────────


def test_keep_last_on_a_protected_list_trims_terminal_items_only():
    value = {"gaps": _gaps(["queued", "merged", "queued", "closed", "queued"])}
    dropped: dict[str, list] = {}
    out, changes = apply_list_ops(
        value,
        keep_last={"gaps": 2},
        dropped=dropped,
        protect=retention_protect({"keep_last": 50, "archive_when": TERMINAL}, value),
    )
    assert [g["n"] for g in out["gaps"]] == [0, 2, 4]
    assert [g["n"] for g in dropped["gaps"]] == [1, 3]
    assert changes["gaps"]["trimmed"] == 2 and changes["gaps"]["kept_open"] == 1


@pytest.mark.asyncio
async def test_a_keep_last_blocked_by_open_items_is_refused_naming_them(monkeypatch, limits):
    # Gap shared-state-trim-after-size-check: keep_last_json kept the open items
    # archive_when protects, the value stayed over the hard limit and the refusal
    # advised the very keep_last_json the write carried.
    stored = _gaps(["queued"] * 6)
    limits(value_size({"gaps": stored}) + 10)
    state = _state({"gaps": stored})
    state.retention = {"keep_last": 50, "archive_when": TERMINAL}
    session = _Session(state)
    _sessions(monkeypatch, session)
    with pytest.raises(ValueError) as refused:
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-gaps",
            append={"gaps": [{"n": 9, "state": "queued", "text": "y" * 40}]},
            keep_last={"gaps": 3},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    message = str(refused.value)
    assert "hard limit" in message
    assert "open item(s) of 'gaps'" in message and "archive_when" in message
    assert "keep_last_json with archive_key" not in message
    assert session.committed is False


def test_the_hard_limit_refusal_without_kept_open_items_keeps_its_advice(limits):
    limits(100)
    big = {"gaps": _gaps(["queued"] * 5)}
    message = shared_state_service.size_limit_refusal(0, big, {"gaps": 0})
    assert message is not None and "keep_last_json with archive_key" in message


def test_keep_last_without_protection_is_unchanged():
    value = {"gaps": _gaps(["queued", "merged", "queued"])}
    out, changes = apply_list_ops(value, keep_last={"gaps": 1}, protect=retention_protect({"keep_last": 5}, value))
    assert [g["n"] for g in out["gaps"]] == [2]
    assert changes["gaps"]["trimmed"] == 2 and "kept_open" not in changes["gaps"]


# ── usage and the 80% warning ──────────────────────────────────────────────


def test_usage_warns_at_80_percent_of_the_cap():
    value = {"gaps": _gaps(["queued"] * 4)}
    size = value_size(value)
    assert retention_usage(value, None) is None
    low = retention_usage(value, {"max_chars": size * 2})
    assert low["percent"] == 50 and "warning" not in low
    high = retention_usage(value, {"max_chars": size + 10, "archive_when": TERMINAL})
    assert high["percent"] >= 80 and "open items stay" in high["warning"]
    by_age = retention_usage(value, {"keep_last": 5})
    assert "gaps 4 of 5 items" in by_age["warning"] and "OLDEST" in by_age["warning"]


# ── the write ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_append_at_the_cap_archives_only_terminal_items(monkeypatch, limits):
    limits(0)
    source = _state({"gaps": _gaps(["queued", "merged", "queued", "closed"])})
    source.retention = {"keep_last": 4, "archive_key": "gaps-archive", "archive_when": TERMINAL}
    archive = _state({})
    archive.key = "gaps-archive"
    _sessions(monkeypatch, _Session(source), _Session(archive))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"n": 9, "state": "queued"}, {"n": 10, "state": "queued"}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert [g["n"] for g in out["value"]["gaps"]] == [0, 2, 9, 10]
    assert [g["n"] for g in archive.value["gaps"]] == [1, 3]
    assert out["rolled"] == {"gaps": 2} and "retention_held_open" not in out
    assert "retention_rolled_by_age" not in out


@pytest.mark.asyncio
async def test_an_append_on_open_items_only_moves_nothing_and_warns(monkeypatch, limits):
    limits(0)
    source = _state({"gaps": _gaps(["queued"] * 4)})
    source.retention = {"keep_last": 4, "archive_key": "gaps-archive", "archive_when": TERMINAL}
    _sessions(monkeypatch, _Session(source))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"n": 9, "state": "queued"}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert [g["n"] for g in out["value"]["gaps"]] == [0, 1, 2, 3, 9]
    assert out.get("rolled") is None
    assert out["retention_held_open"] == {"gaps": 1}
    assert "warning" in out["retention_usage"]


@pytest.mark.asyncio
async def test_a_policy_without_archive_when_still_rolls_by_age_and_flags_it(monkeypatch, limits):
    limits(0)
    source = _state({"gaps": _gaps(["queued"] * 4)})
    source.retention = {"keep_last": 4}
    _sessions(monkeypatch, _Session(source))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"n": 9, "state": "queued"}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert [g["n"] for g in out["value"]["gaps"]] == [1, 2, 3, 9]
    assert out["rolled"] == {"gaps": 1} and out["retention_rolled_by_age"] is True
