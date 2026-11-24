"""Retention policy of a shared state entry (#11839).

A key may carry a policy (``keep_last`` / ``max_chars`` with an
``archive_key``) applied on every write: an append on a list at its size cap
lands, the oldest items roll into the archive key, and an archive at its own
cap drops its oldest entries instead of refusing the roll.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.shared_state import (
    ARCHIVE_STAMP,
    fit_archive,
    retention_shape,
    roll_lists,
    value_size,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _call, _Session, _state


@pytest.fixture
def limits(monkeypatch):
    def _set(hard: int, max_items: int = 0) -> None:
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_soft_limit_chars", 0)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_hard_limit_chars", hard)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_list_max_items", max_items)

    return _set


def _sessions(monkeypatch, *sessions):
    """``async_session`` answers the given sessions in order: the source write, then its archive."""
    queue = list(sessions)
    monkeypatch.setattr("app.core.database.async_session", lambda: queue.pop(0))


def _runs(start: int, count: int, width: int = 40) -> list[dict]:
    return [{"n": i, "text": "x" * width} for i in range(start, start + count)]


# ── the policy shape ───────────────────────────────────────────────────────


def test_a_policy_needs_a_bound_and_takes_known_fields_only():
    assert retention_shape({"keep_last": 3, "archive_key": " a "}) == {"keep_last": 3, "archive_key": "a"}
    assert retention_shape({}) == {}
    for bad in ({"archive_key": "a"}, {"keep_last": 0}, {"max_chars": True}, {"keep": 3}, {"keep_last": 2, "lists": []}):
        with pytest.raises(ValueError):
            retention_shape(bad)


def test_roll_lists_keeps_the_newest_items_and_never_the_appended_ones(limits):
    limits(0)
    value = {"runs": _runs(0, 6), "other": 1}
    rolled = roll_lists(value, {"keep_last": 4}, {"runs": 2})
    assert [r["n"] for r in value["runs"]] == [2, 3, 4, 5]
    assert [r["n"] for r in rolled["runs"]] == [0, 1]
    # The appended items stay whatever the bound.
    value = {"runs": _runs(0, 6)}
    roll_lists(value, {"keep_last": 1}, {"runs": 3})
    assert [r["n"] for r in value["runs"]] == [3, 4, 5]


def test_roll_lists_rolls_the_oldest_until_the_value_fits_max_chars(limits):
    limits(0)
    value = {"runs": _runs(0, 10)}
    rolled = roll_lists(value, {"max_chars": 300}, {"runs": 1})
    assert value_size(value) <= 300
    assert value["runs"][-1]["n"] == 9
    assert [r["n"] for r in rolled["runs"]] == list(range(len(rolled["runs"])))


# ── the acceptance: append past the cap ───────────────────────────────────


@pytest.mark.asyncio
async def test_an_append_past_the_cap_lands_and_the_oldest_move_to_the_archive(monkeypatch, limits):
    limits(600)
    old = _runs(0, 9)
    assert value_size({"runs": old}) <= 600 < value_size({"runs": old + _runs(100, 3)})
    source = _state({"runs": old})
    source.retention = {"max_chars": 600, "archive_key": "metrics-archive"}
    archive = _state({})
    archive.key = "metrics-archive"
    source_session, archive_session = _Session(source), _Session(archive)
    _sessions(monkeypatch, source_session, archive_session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"runs": _runs(100, 3)},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert source_session.committed and archive_session.committed
    runs = out["value"]["runs"]
    assert [r["n"] for r in runs[-3:]] == [100, 101, 102]
    assert value_size(out["value"]) <= 600
    moved = archive.value["runs"]
    assert out["rolled"] == {"runs": len(moved)} and out["rolled_to"] == "metrics-archive"
    assert [r["n"] for r in moved] == list(range(len(moved)))  # the oldest, in order
    assert all(ARCHIVE_STAMP in r for r in moved)
    assert {r["n"] for r in moved} | {r["n"] for r in runs} == set(range(9)) | {100, 101, 102}


@pytest.mark.asyncio
async def test_an_archive_at_its_cap_drops_its_oldest_and_the_roll_still_lands(monkeypatch, limits):
    limits(600)
    source = _state({"runs": _runs(0, 9)})
    source.retention = {"max_chars": 600, "archive_key": "metrics-archive"}
    stamp = "2026-11-01T00:00:00+00:00"
    archive = _state({"runs": [{**r, ARCHIVE_STAMP: stamp} for r in _runs(500, 9)]})
    archive.key = "metrics-archive"
    _sessions(monkeypatch, _Session(source), _Session(archive))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"runs": _runs(100, 3)},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["archive_dropped"] > 0
    assert value_size(archive.value) <= 600
    kept = [r["n"] for r in archive.value["runs"]]
    assert 500 not in kept  # the oldest archived entry went first
    assert kept[-1] == out["rolled"]["runs"] - 1  # the newest rolled item is kept


def test_fit_archive_drops_the_oldest_rolled_items_when_they_alone_do_not_fit(limits):
    limits(300)
    value = {"runs": _runs(0, 20)}
    dropped = fit_archive(value, {"runs": 20})
    assert dropped > 0 and value_size(value) <= 300
    assert value["runs"][-1]["n"] == 19


@pytest.mark.asyncio
async def test_without_an_archive_key_the_rolled_items_are_dropped_and_counted(monkeypatch, limits):
    limits(0)
    source = _state({"runs": _runs(0, 5)})
    source.retention = {"keep_last": 5}
    _sessions(monkeypatch, _Session(source))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"runs": _runs(100, 2)},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert [r["n"] for r in out["value"]["runs"]] == [2, 3, 4, 100, 101]
    assert out["rolled"] == {"runs": 2} and out["rolled_to"] is None


@pytest.mark.asyncio
async def test_a_guarded_roll_without_an_archive_key_dropping_more_than_appended_is_refused(monkeypatch, limits):
    """A policy set on a list already over its bound drops nothing silently (item ba7836c9)."""
    limits(0)
    source = _state({"runs": _runs(0, 10)})
    _sessions(monkeypatch, _Session(source))
    with pytest.raises(ValueError, match="no archive_key"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"runs": _runs(100, 1)},
            retention={"keep_last": 3},
            guard_trim=True,
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert [r["n"] for r in source.value["runs"]] == list(range(10))
    # Confirmed, the same write drops them and says so.
    source = _state({"runs": _runs(0, 10)})
    _sessions(monkeypatch, _Session(source))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"runs": _runs(100, 1)},
        retention={"keep_last": 3},
        guard_trim=False,
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert [r["n"] for r in out["value"]["runs"]] == [8, 9, 100]
    assert out["rolled"] == {"runs": 8}


@pytest.mark.asyncio
async def test_setting_a_policy_stores_it_and_rolls_the_list_at_once(monkeypatch, limits):
    limits(0)
    source = _state({"runs": _runs(0, 5)})
    archive = _state({})
    _sessions(monkeypatch, _Session(source), _Session(archive))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        retention={"keep_last": 3, "archive_key": "runs-archive"},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert source.retention == {"keep_last": 3, "archive_key": "runs-archive"}
    assert out["retention"] == source.retention
    assert [r["n"] for r in out["value"]["runs"]] == [2, 3, 4]
    assert [r["n"] for r in archive.value["runs"]] == [0, 1]


@pytest.mark.asyncio
async def test_a_policy_archiving_into_its_own_key_is_refused(monkeypatch, limits):
    limits(0)
    session = _Session(_state({"runs": []}))
    _sessions(monkeypatch, session)
    with pytest.raises(ValueError, match="another key"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            retention={"keep_last": 3, "archive_key": "open-prs"},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert session.committed is False


# ── the tool ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_passes_the_policy_and_reads_back_what_rolled(monkeypatch):
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {
            "key": kwargs["key"],
            "value": {"runs": [{"n": 9}]},
            "notes": [],
            "retention": kwargs["retention"],
            "list_changes": {"runs": {"appended": 1, "removed": 0}},
            "rolled": {"runs": 4},
            "rolled_to": kwargs["retention"]["archive_key"],
            "archive_dropped": 2,
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="metrics",
        append_json='{"runs": [{"n": 9}]}',
        retention_json=json.dumps({"max_chars": 60000, "archive_key": "metrics-archive"}),
    )
    assert not result.is_error, result.content
    assert calls[0]["retention"]["archive_key"].endswith("metrics-archive")
    assert "rolled by retention to `metrics-archive`: runs 4" in result.content
    assert "dropped its 2 oldest" in result.content


@pytest.mark.asyncio
async def test_the_tool_refuses_a_policy_onto_its_own_key(monkeypatch):
    async def _upsert(**_kwargs):
        raise AssertionError("nothing may be written")

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor, "set_shared_state", key="metrics", retention_json='{"keep_last": 3, "archive_key": "metrics"}'
    )
    assert "must name another key" in result.content


def test_the_tool_schema_declares_retention_json():
    tools, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    schema = next(t for t in tools if t.name == "set_shared_state").input_schema
    assert "retention_json" in schema["properties"]
