"""Size limits and archive retention of shared state (#10789).

A value over the soft limit is written and its read-back warns; over the hard
limit a write is refused unless it shrinks the value; an archive write drops
its oldest items instead of passing the hard limit; the archive retention
sweep drops platform-archived items past ``shared_state_archive_retention_days``.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.shared_state import (
    ARCHIVE_STAMP,
    drop_archived_before,
    serialize_state,
    size_limit_refusal,
    size_warning,
    value_size,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _call, _Session, _state


@pytest.fixture
def limits(monkeypatch):
    def _set(soft: int, hard: int) -> None:
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_soft_limit_chars", soft)
        monkeypatch.setattr(shared_state_service.settings, "shared_state_value_hard_limit_chars", hard)

    return _set


def test_a_value_under_both_limits_has_no_warning_and_no_refusal(limits):
    limits(1000, 5000)
    value = {"runs": [{"n": 1}]}
    assert size_warning(value) is None
    assert size_limit_refusal(0, value) is None


def test_the_warning_names_the_largest_keys_and_the_fix(limits):
    limits(100, 0)
    value = {"runs": [{"n": i, "text": "x" * 20} for i in range(10)], "small": 1}
    warning = size_warning(value)
    assert warning is not None
    assert "runs" in warning and "(10 items)" in warning
    assert "keep_last_json" in warning and "archive_key" in warning


def test_the_hard_limit_refuses_a_growing_value_and_admits_a_shrinking_one(limits):
    limits(0, 200)
    big = {"runs": [{"n": i, "text": "x" * 20} for i in range(20)]}
    assert size_limit_refusal(0, big) is not None
    # Already over the limit and shrinking: the compaction must land.
    smaller = {"runs": big["runs"][5:]}
    assert size_limit_refusal(value_size(big), smaller) is None


def test_zero_disables_both_limits(limits):
    limits(0, 0)
    big = {"runs": ["x" * 10_000]}
    assert size_warning(big) is None
    assert size_limit_refusal(0, big) is None


@pytest.mark.asyncio
async def test_upsert_refuses_a_write_over_the_hard_limit_and_commits_nothing(monkeypatch, limits):
    limits(0, 300)
    session = _Session(_state({"runs": []}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="hard limit"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"runs": [{"n": i, "text": "x" * 40} for i in range(20)]},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert session.committed is False


@pytest.mark.asyncio
async def test_upsert_over_the_soft_limit_writes_and_warns(monkeypatch, limits):
    limits(100, 0)
    session = _Session(_state({"runs": []}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"runs": [{"n": i, "text": "x" * 40} for i in range(5)]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert session.committed is True
    assert "soft limit" in out["size_warning"]
    assert out["over_soft_limit"] is True
    assert out["size_chars"] == value_size(out["value"])
    assert out["largest_list"] == 5


@pytest.mark.asyncio
async def test_an_archive_write_stamps_its_items_and_drops_the_oldest_past_the_hard_limit(monkeypatch, limits):
    limits(0, 600)
    old = [{"n": i, "text": "x" * 40} for i in range(10)]
    session = _Session(_state({"runs": old}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"runs": [{"n": 100 + i, "text": "y" * 40} for i in range(5)]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
        archive=True,
    )
    runs = out["value"]["runs"]
    assert value_size(out["value"]) <= 600
    assert out["archive_trimmed"] > 0
    assert runs[-1]["n"] == 104 and ARCHIVE_STAMP in runs[-1]
    assert runs[0]["n"] != 0  # the oldest went first


@pytest.mark.asyncio
async def test_an_archive_write_never_drops_the_items_it_archives(monkeypatch, limits):
    """#10813: the trim takes the older items of every list, never the appended ones."""
    limits(0, 900)
    stamp = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    entries = [{"n": i, "text": "e" * 40, ARCHIVE_STAMP: stamp} for i in range(10)]
    session = _Session(_state({"gaps": [], "entries": entries}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    fresh = [{"n": 100 + i, "text": "g" * 40} for i in range(5)]
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="gaps-archive",
        append={"gaps": fresh},
        writer=COMPANY_AGENT,
        is_company_managed=True,
        archive=True,
    )
    assert [g["n"] for g in out["value"]["gaps"]] == [100, 101, 102, 103, 104]
    assert value_size(out["value"]) <= 900
    assert out["archive_trimmed"] > 0
    assert out["value"]["entries"][0]["n"] > 0  # the older list paid for it, oldest first


@pytest.mark.asyncio
async def test_an_archive_write_that_cannot_fit_is_refused_and_commits_nothing(monkeypatch, limits):
    limits(0, 300)
    session = _Session(_state({"gaps": []}))
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="cannot hold these items"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="gaps-archive",
            append={"gaps": [{"n": i, "text": "g" * 40} for i in range(20)]},
            writer=COMPANY_AGENT,
            is_company_managed=True,
            archive=True,
        )
    assert session.committed is False


def test_drop_archived_before_keeps_unstamped_and_recent_items():
    now = datetime.now(UTC)
    value = {
        "runs": [
            {"n": 1, ARCHIVE_STAMP: (now - timedelta(days=40)).isoformat()},
            {"n": 2, ARCHIVE_STAMP: (now - timedelta(days=2)).isoformat()},
            {"n": 3, "archived_at": (now - timedelta(days=90)).isoformat()},
            {"n": 4, ARCHIVE_STAMP: "not a date"},
        ],
        "other": 1,
    }
    out, dropped = drop_archived_before(value, now - timedelta(days=30))
    assert dropped == 1
    assert [r["n"] for r in out["runs"]] == [2, 3, 4]
    assert out["other"] == 1


def test_serialize_state_carries_the_size_flags(limits):
    limits(10, 0)
    state = _state({"runs": [1, 2, 3], "x": "abcdefghij"})
    state.held_by_run_id = None
    out = serialize_state(state)
    assert out["size_chars"] == value_size(state.value)
    assert out["largest_list"] == 3
    assert out["over_soft_limit"] is True


@pytest.mark.asyncio
async def test_the_listing_shows_the_size_and_the_oversize_flag(monkeypatch):
    async def _list(**_kwargs):
        return [
            {"key": "big", "status": "active", "size_chars": 900_000, "largest_list": 4000, "over_soft_limit": True},
            {"key": "small", "status": "active", "size_chars": 12, "largest_list": 0},
        ]

    async def _count(**_kwargs):
        return 2

    from app.services import coordination_scope

    async def _scope(*_args):
        return SimpleNamespace(label=lambda _w: "", shared_state_clause=lambda: None)

    monkeypatch.setattr(shared_state_service, "list_states", _list)
    monkeypatch.setattr(shared_state_service, "count_states", _count)
    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _scope)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_shared_state")
    assert not result.is_error, result.content
    assert "`big` [active, OVERSIZE] 900000 chars, longest list 4000; ends: not set" in result.content
    assert "`small` [active] 12 chars; ends: not set" in result.content


@pytest.mark.asyncio
async def test_the_retention_sweep_is_off_at_zero(monkeypatch):
    from app.services import autonomous

    monkeypatch.setattr(autonomous.settings, "shared_state_archive_retention_days", 0)

    async def _boom(**_kwargs):
        raise AssertionError("purge ran with retention disabled")

    monkeypatch.setattr(shared_state_service, "purge_archived_items", _boom)
    assert await autonomous._cleanup_archived_shared_state_items() == 0
