"""File retention: the days per file category, the sweep, the tombstone and what a deleted file answers.

The agent's scratch (``agent_working``) and delivered (``agent_download``)
files go 60 days after their last use; every category's days are stored in
Settings > Storage & retention, seeded from the environment, an explicit
environment variable locking it. A file a live object names is kept; a deleted
one keeps its row (a tombstone) so the download routes answer 410 Gone and the
tools a clear sentence, never an exception.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.llm.base import FileRef
from app.services import file_retention
from app.services.file_retention import (
    BY_KEY,
    CATEGORIES,
    DELETED_ADMIN,
    DELETED_RETENTION,
    MAX_DAYS,
    RetentionLockedError,
    category_of,
    cutoff,
    deleted_notice,
    expires_at,
    gone_detail,
    policy_of,
    raise_if_deleted,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _row(**fields):
    base = {
        "id": uuid.uuid4(),
        "filename": "notes.md",
        "content_type": "text/markdown",
        "size": 2048,
        "s3_object_name": "generated/notes.md",
        "source": "agent_working",
        "description": None,
        "created_at": NOW - timedelta(days=90),
        "last_accessed_at": None,
        "deleted_at": None,
        "deleted_reason": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


# -- the policy ------------------------------------------------------------------


def test_scratch_and_download_files_default_to_sixty_days_after_last_use():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["file_retention_agent_working_days"].default == 60
    assert fields["file_retention_agent_download_days"].default == 60
    assert BY_KEY["agent_working"].basis == "last_use"
    assert BY_KEY["agent_download"].basis == "last_use"


def test_categories_with_no_limit_today_stay_never_by_default():
    from app.core.config import Settings

    for name in ("file_retention_agent_days", "file_retention_chat_days", "file_retention_channel_days"):
        assert Settings.model_fields[name].default == 0


def test_every_category_maps_its_sources():
    assert category_of("agent_working").key == "agent_working"
    assert category_of("agent_download").key == "agent_download"
    assert category_of("chat").key == "chat"
    assert category_of("channel:teams").key == "channel"
    assert category_of("agent_check").key == "agent_check"
    assert category_of("template_asset") is None
    assert len({c.key for c in CATEGORIES}) == len(CATEGORIES)


def test_a_stored_value_wins_over_the_default(monkeypatch):
    for category in CATEGORIES:
        monkeypatch.delenv(category.env_var, raising=False)
    rules = policy_of({"agent_working": 30, "chat": 365})
    assert rules["agent_working"].days == 30
    assert rules["agent_working"].source == "settings"
    assert rules["chat"].days == 365
    assert rules["agent_download"].source == "default"
    assert rules["agent_download"].days == 60


def test_an_environment_variable_set_explicitly_locks_the_category(monkeypatch):
    monkeypatch.setenv("FILE_RETENTION_AGENT_WORKING_DAYS", "90")
    with patch("app.core.config.settings.file_retention_agent_working_days", 90):
        rule = policy_of({"agent_working": 10})["agent_working"]
    assert rule.locked and rule.days == 90 and rule.source == "environment"
    assert rule.as_dict()["locked"] is True


def test_stored_values_are_bounded():
    rules = policy_of({"agent_working": 10**9, "chat": -4, "agent": "x"})
    assert rules["agent_working"].days == MAX_DAYS
    assert rules["chat"].days == 0
    assert rules["agent"].source == "default"


@pytest.mark.asyncio
async def test_saving_a_locked_category_is_refused(monkeypatch):
    monkeypatch.setenv("FILE_RETENTION_CHAT_DAYS", "0")
    row = SimpleNamespace(file_retention=None)
    with (
        patch("app.api.v1.platform_settings_helpers.get_platform_settings", AsyncMock(return_value=row)),
        pytest.raises(RetentionLockedError),
    ):
        await file_retention.save_policy(SimpleNamespace(), {"chat": 30})
    assert row.file_retention is None


@pytest.mark.asyncio
async def test_saving_reports_what_changed_and_refuses_bad_values(monkeypatch):
    for category in CATEGORIES:
        monkeypatch.delenv(category.env_var, raising=False)
    row = SimpleNamespace(file_retention={"agent_working": 60})
    with patch("app.api.v1.platform_settings_helpers.get_platform_settings", AsyncMock(return_value=row)):
        changed = await file_retention.save_policy(SimpleNamespace(), {"agent_working": 30, "chat": 0})
        assert changed == {"agent_working": {"from": 60, "to": 30}}
        assert row.file_retention == {"agent_working": 30, "chat": 0}
        with pytest.raises(ValueError):
            await file_retention.save_policy(SimpleNamespace(), {"chat": MAX_DAYS + 1})
        with pytest.raises(ValueError):
            await file_retention.save_policy(SimpleNamespace(), {"chat": True})
        with pytest.raises(KeyError):
            await file_retention.save_policy(SimpleNamespace(), {"nope": 3})


# -- the clock -------------------------------------------------------------------


def test_cutoff_keeps_never_and_the_one_hour_floor():
    assert cutoff(0, now=NOW) is None
    assert cutoff(60, now=NOW) == NOW - timedelta(days=60)


def test_expiry_counts_from_the_last_use_and_renews_on_use():
    row = _row(created_at=NOW - timedelta(days=100), last_accessed_at=NOW - timedelta(days=10))
    assert expires_at(row, 60, "last_use") == NOW + timedelta(days=50)
    assert expires_at(_row(last_accessed_at=None), 60, "last_use") == NOW - timedelta(days=30)
    assert expires_at(row, 60, "created") == NOW - timedelta(days=40)
    assert expires_at(row, 0, "last_use") is None
    assert expires_at(_row(deleted_at=NOW), 60, "last_use") is None


# -- the sweep -------------------------------------------------------------------


def _rows(*ids, size=100):
    return [(fid, NOW - timedelta(days=90), size) for fid in ids]


@pytest.mark.asyncio
async def test_the_sweep_keeps_referenced_files_and_tombstones_the_rest():
    kept, gone = uuid.uuid4(), uuid.uuid4()
    db = SimpleNamespace(commit=AsyncMock())
    due = AsyncMock(side_effect=[_rows(kept, gone), []])
    tombstone = AsyncMock(return_value=["generated/gone"])
    purge = AsyncMock(return_value=1)
    with (
        patch.object(file_retention, "due", due),
        patch.object(file_retention, "referenced", AsyncMock(return_value={str(kept)})),
        patch.object(file_retention, "tombstone", tombstone),
        patch("app.services.file_cleanup.purge_storage_objects", purge),
    ):
        tally = await file_retention.sweep_category(db, BY_KEY["agent_working"], 60, now=NOW)
    assert tally["files"] == 1 and tally["bytes"] == 100 and tally["kept_referenced"] == 1
    assert tombstone.await_args.args[1] == [gone]
    assert tombstone.await_args.args[2] == DELETED_RETENTION
    db.commit.assert_awaited()  # rows committed BEFORE the bytes go
    purge.assert_awaited_once_with(["generated/gone"])


@pytest.mark.asyncio
async def test_a_dry_run_counts_and_writes_nothing():
    db = SimpleNamespace(commit=AsyncMock())
    tombstone = AsyncMock()
    with (
        patch.object(file_retention, "due", AsyncMock(side_effect=[_rows(uuid.uuid4(), uuid.uuid4(), size=5), []])),
        patch.object(file_retention, "referenced", AsyncMock(return_value=set())),
        patch.object(file_retention, "tombstone", tombstone),
    ):
        tally = await file_retention.sweep_category(db, BY_KEY["agent_download"], 60, now=NOW, dry_run=True)
    assert tally["files"] == 2 and tally["bytes"] == 10
    tombstone.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_tick_is_bounded_and_says_more_remain():
    batch = [_rows(uuid.uuid4())[0] for _ in range(3)]
    with (
        patch.object(file_retention, "due", AsyncMock(return_value=batch)),
        patch.object(file_retention, "referenced", AsyncMock(return_value=set())),
    ):
        tally = await file_retention.sweep_category(
            SimpleNamespace(commit=AsyncMock()), BY_KEY["agent_working"], 60, now=NOW, dry_run=True, limit=3
        )
    assert tally["more"] is True and tally["files"] == 3


@pytest.mark.asyncio
async def test_the_sweep_skips_never_and_the_categories_another_sweep_owns(monkeypatch):
    for category in CATEGORIES:
        monkeypatch.delenv(category.env_var, raising=False)
    seen = []

    async def fake(_db, category, days, **_kw):
        seen.append((category.key, days))
        return {"files": 0, "bytes": 0, "kept_referenced": 0, "more": False}

    with (
        patch.object(file_retention, "load_policy", AsyncMock(return_value=policy_of({}))),
        patch.object(file_retention, "sweep_category", fake),
        patch.object(file_retention, "_remember_sweep", AsyncMock()),
    ):
        await file_retention.sweep(SimpleNamespace())
    assert seen == [("agent_working", 60), ("agent_download", 60)]


# -- the deleted file ------------------------------------------------------------


def test_the_notice_names_the_date_and_the_retention():
    row = _row(deleted_at=NOW, deleted_reason=DELETED_RETENTION)
    assert deleted_notice(row) == "notes.md was deleted on 2026-11-19 by the retention policy (unused for too long)."
    chat = _row(source="chat", deleted_at=NOW, deleted_reason=DELETED_RETENTION)
    assert deleted_notice(chat).endswith("by the retention policy.")
    assert "administrator" in deleted_notice(_row(deleted_at=NOW, deleted_reason=DELETED_ADMIN))


def test_the_download_route_answers_410_gone_with_a_json_reason():
    row = _row(deleted_at=NOW, deleted_reason=DELETED_RETENTION)
    with pytest.raises(HTTPException) as caught:
        raise_if_deleted(row)
    assert caught.value.status_code == 410
    assert caught.value.detail["code"] == "file_deleted"
    assert caught.value.detail == gone_detail(row)
    raise_if_deleted(_row())  # a live file passes


def test_a_tombstone_file_ref_carries_its_notice():
    ref = FileRef.from_attachment(_row(deleted_at=NOW, deleted_reason=DELETED_RETENTION))
    assert ref.is_deleted and "retention policy" in ref.deleted_notice
    assert not FileRef.from_attachment(_row()).is_deleted


@pytest.mark.asyncio
async def test_reading_a_deleted_file_raises_the_notice_never_a_storage_error():
    from app.services.file_storage import FileDeletedError, FileNotFoundInStorageError
    from app.tools.base import FileContext

    ref = FileRef.from_attachment(_row(deleted_at=NOW, deleted_reason=DELETED_RETENTION))
    context = FileContext()
    context.add(ref)
    download = AsyncMock()
    with patch("app.services.file_storage.file_storage.download", download), pytest.raises(FileDeletedError) as caught:
        await context.read_bytes(ref.file_id)
    assert isinstance(caught.value, FileNotFoundInStorageError)
    assert "retention policy" in str(caught.value)
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_model_reads_the_notice_for_a_deleted_attachment():
    from app.llm.file_resolver import _extract_fallback

    ref = FileRef.from_attachment(_row(deleted_at=NOW, deleted_reason=DELETED_RETENTION))
    fallback = await _extract_fallback(ref)
    assert "retention policy" in fallback.text


def test_available_files_marks_a_deleted_file():
    from app.tools.base import FileContext

    context = FileContext()
    context.add(FileRef.from_attachment(_row(deleted_at=NOW, deleted_reason=DELETED_RETENTION)))
    assert "DELETED (content gone)" in context.to_prompt_section()


# -- the Files page --------------------------------------------------------------


def test_the_files_page_shows_when_an_unused_file_expires_and_a_tombstone(monkeypatch):
    from app.api.v1.files import _serialize

    for category in CATEGORIES:
        monkeypatch.delenv(category.env_var, raising=False)
    me = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    rules = policy_of({})
    live = _row(
        user_id=me.id,
        conversation_id=None,
        assignment_run_id=None,
        message_id=None,
        last_accessed_at=NOW - timedelta(days=10),
    )
    item = _serialize(live, me, {}, {}, rules)
    assert item.expires_at == (NOW + timedelta(days=50)).isoformat()
    assert item.deleted_at is None
    gone = _row(
        user_id=me.id,
        conversation_id=None,
        assignment_run_id=None,
        message_id=None,
        deleted_at=NOW,
        deleted_reason=DELETED_RETENTION,
    )
    item = _serialize(gone, me, {}, {}, rules)
    assert item.deleted_reason == DELETED_RETENTION and item.expires_at is None


# -- what keeps a file -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_a_video_or_a_workset_names_is_kept():
    video, workset, free = (str(uuid.uuid4()) for _ in range(3))
    scalars = MagicMock()
    scalars.scalars.return_value = [uuid.UUID(workset)]
    counts = MagicMock()
    counts.scalar.return_value = 0
    db = SimpleNamespace(execute=AsyncMock(side_effect=[scalars] + [counts] * 20))
    with patch("app.services.video_studio.retention.files_in_use", AsyncMock(return_value={video})):
        kept = await file_retention.referenced(db, [video, workset, free])
    assert kept == {video, workset}
