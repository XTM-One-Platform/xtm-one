import uuid

import pytest

from app.services.sub_agent import (
    get_task_result,
    get_task_result_by_id,
    get_task_statuses,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_task_id", ["not-a-uuid", "", None, 12345])
async def test_get_task_statuses_returns_none_for_invalid_task_id(monkeypatch, bad_task_id):
    def _unexpected_session():
        raise AssertionError("async_session should not be created for invalid UUID task_id")

    monkeypatch.setattr("app.core.database.async_session", _unexpected_session)

    statuses = await get_task_statuses(
        [bad_task_id],
        user_id=str(uuid.uuid4()),
    )

    assert all(v is None for v in statuses.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_task_id", ["not-a-uuid", "", None, 12345])
async def test_get_task_result_returns_none_for_invalid_task_id(monkeypatch, bad_task_id):
    def _unexpected_session():
        raise AssertionError("async_session should not be created for invalid UUID task_id")

    monkeypatch.setattr("app.core.database.async_session", _unexpected_session)

    result = await get_task_result(
        db=None,
        task_id=bad_task_id,
        user_id=str(uuid.uuid4()),
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_task_result_returns_none_for_invalid_user_id(monkeypatch):
    def _unexpected_session():
        raise AssertionError("async_session should not be created for invalid UUID user_id")

    monkeypatch.setattr("app.core.database.async_session", _unexpected_session)

    result = await get_task_result(
        db=None,
        task_id=str(uuid.uuid4()),
        user_id="not-a-uuid",
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_task_id", [None, 12345])
async def test_get_task_result_by_id_returns_none_for_non_string_inputs(monkeypatch, bad_task_id):
    def _unexpected_session():
        raise AssertionError("async_session should not be created for invalid UUID task_id")

    monkeypatch.setattr("app.core.database.async_session", _unexpected_session)

    result = await get_task_result_by_id(bad_task_id)

    assert result is None
