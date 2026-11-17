"""A quarantined KB sync source notifies its knowledge base owner, and every
administrator as well when the knowledge base is company-managed."""

import json
import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.activity import Activity
from app.services import kb_sync_scheduler


class _Scalar:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Row:
    def __init__(self, row: tuple[str, bool]) -> None:
        self._row = row

    def one_or_none(self) -> tuple[str, bool]:
        return self._row


class _Ids:
    def __init__(self, ids: list[uuid.UUID]) -> None:
        self._ids = ids

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: self._ids)


class _Nested:
    async def __aenter__(self) -> "_Nested":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _db(
    owner_id: uuid.UUID | None,
    *,
    name: str,
    company_managed: bool,
    admin_ids: Sequence[uuid.UUID] = (),
) -> Any:
    """Answers the owner lookup, then the KB name and flag, then the admins."""
    results: list[object] = [_Scalar(owner_id), _Row((name, company_managed))]
    if company_managed:
        results.append(_Ids(list(admin_ids)))
    return SimpleNamespace(
        flush=AsyncMock(),
        begin_nested=_Nested,
        execute=AsyncMock(side_effect=results),
        add=Mock(),
    )


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    sent: list[tuple[str, dict[str, Any]]] = []

    class _Redis:
        async def publish(self, channel: str, payload: str) -> None:
            sent.append((channel, json.loads(payload)))

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_Redis()))
    return sent


def _notified(db: Any) -> list[Activity]:
    return [a for (a,), _kw in db.add.call_args_list if isinstance(a, Activity)]


async def _quarantine(db: Any, kb_id: uuid.UUID) -> None:
    await kb_sync_scheduler._emit_perm_fail_quarantine_alert(
        db,
        kb_id=kb_id,
        source_type="github_public",
        perm_fail_count=10,
        quarantine_message="Source needs reconfiguration. Last error: 404 Not Found",
    )


@pytest.mark.asyncio
async def test_a_personal_kb_quarantine_notifies_its_owner_once(
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    owner_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    db = _db(owner_id, name="Runbooks", company_managed=False)

    await _quarantine(db, kb_id)

    notified = _notified(db)
    assert [a.user_id for a in notified] == [owner_id]
    assert notified[0].activity_type == "notification"
    extra = cast(dict[str, Any], notified[0].extra_data)
    assert extra["kb_id"] == str(kb_id)
    assert extra["kb_event"] == "sync_quarantined"
    # Which knowledge base and that the source is disabled; the raw error is
    # served per caller on the source, never in a notification.
    summary = notified[0].summary or ""
    assert "Runbooks" in summary
    assert "disabled" in summary
    assert "404" not in summary
    assert [ch for ch, _ in published] == ["activity:company", f"activity:{owner_id}"]


@pytest.mark.asyncio
async def test_a_company_kb_quarantine_notifies_the_owner_and_each_admin_once(
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    owner_id = uuid.uuid4()
    other_admin = uuid.uuid4()
    db = _db(
        owner_id,
        name="Company wiki",
        company_managed=True,
        # The owner is an administrator too: notified once, not twice.
        admin_ids=[owner_id, other_admin],
    )

    await _quarantine(db, uuid.uuid4())

    assert [a.user_id for a in _notified(db)] == [owner_id, other_admin]
    personal = [ch for ch, _ in published if ch != "activity:company"]
    assert personal == [f"activity:{owner_id}", f"activity:{other_admin}"]


@pytest.mark.asyncio
async def test_an_ownerless_company_kb_quarantine_still_reaches_the_admins(
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    admin_id = uuid.uuid4()
    db = _db(None, name="Company wiki", company_managed=True, admin_ids=[admin_id])

    await _quarantine(db, uuid.uuid4())

    assert [a.user_id for a in _notified(db)] == [admin_id]


@pytest.mark.asyncio
async def test_the_company_broadcast_never_carries_the_kb_name(
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    db = _db(uuid.uuid4(), name="Runbooks", company_managed=False)

    await _quarantine(db, uuid.uuid4())

    company = [payload for ch, payload in published if ch == "activity:company"]
    assert len(company) == 1
    assert "Runbooks" not in company[0]["summary"]
    assert "404" not in company[0]["summary"]


@pytest.mark.asyncio
async def test_a_failure_alert_reaches_the_owner_only(
    published: list[tuple[str, dict[str, Any]]],
) -> None:
    """Only the quarantine alerts widen the audience to the administrators."""
    owner_id = uuid.uuid4()
    db: Any = SimpleNamespace(
        flush=AsyncMock(),
        begin_nested=_Nested,
        execute=AsyncMock(return_value=_Scalar(owner_id)),
        add=Mock(),
    )

    await kb_sync_scheduler._emit_persistent_failure_alert(
        db,
        kb_id=uuid.uuid4(),
        source_type="github_public",
        failure_count=3,
        error_message="404 Not Found",
    )

    assert [a.user_id for a in _notified(db)] == [owner_id]
    assert db.execute.await_count == 1
