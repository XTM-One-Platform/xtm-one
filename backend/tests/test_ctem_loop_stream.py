# SPDX-License-Identifier: Apache-2.0

"""CTEM loop frames on the activity stream (#838, dev-docs/ctem-loop-visibility.md section 5)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_signal import CtemSignal
from app.services import ctem_loop_stream as stream

_NO_DB = cast(AsyncSession, None)


def _signal(**over: Any) -> CtemSignal:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "state": "new",
        "trigger_path": "new_vulnerability",
        "lens": "world",
        "recorded_by": str(uuid.uuid4()),
        "updated_at": datetime(2026, 11, 21, 8, 0, tzinfo=UTC),
        "last_seen_at": datetime(2026, 11, 21, 7, 0, tzinfo=UTC),
        "subject_ref": "cpe:2.3:a:f5:nginx:1.27.1",
        "subject_label": "NGINX 1.27.1",
        "evidence": {"link": "https://example.invalid/x"},
    }
    base.update(over)
    return cast(CtemSignal, SimpleNamespace(**base))


def test_frame_carries_ids_and_states_only() -> None:
    signal = _signal()
    frame = stream.signal_frame(signal)
    assert frame == {
        "type": "ctem_signal_update",
        "signal_id": str(signal.id),
        "state": "new",
        "trigger_path": "new_vulnerability",
        "lens": "world",
        "at": "2026-11-21T08:00:00+00:00",
    }
    text = json.dumps(frame)
    assert "nginx" not in text.lower()
    assert "example.invalid" not in text


def test_a_person_signal_reaches_its_recorder_and_the_admins_each_once() -> None:
    recorder = str(uuid.uuid4())
    admin = str(uuid.uuid4())
    assert stream.signal_channels(recorder, [admin, recorder]) == [f"activity:{recorder}", f"activity:{admin}"]


def test_a_listener_signal_reaches_the_admins_only_and_never_the_company() -> None:
    admin = str(uuid.uuid4())
    assert stream.signal_channels("system", [admin]) == [f"activity:{admin}"]
    assert "activity:company" not in stream.signal_channels("system", [])


@pytest.mark.asyncio
async def test_a_publish_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> Any:
        raise ConnectionError("redis down")

    async def admins(_db: AsyncSession) -> list[str]:
        return []

    monkeypatch.setattr(stream, "redis_client", broken)
    monkeypatch.setattr(stream, "_admin_ids", admins)
    monkeypatch.setattr(stream, "_down_until", 0.0)
    await stream.publish_signal_update(_NO_DB, _signal())
    assert stream._down_until > 0


@pytest.mark.asyncio
async def test_publish_sends_one_frame_per_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    class FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            sent.append((channel, json.loads(payload)))

    async def client() -> FakeRedis:
        return FakeRedis()

    admin = str(uuid.uuid4())

    async def admins(_db: AsyncSession) -> list[str]:
        return [admin]

    monkeypatch.setattr(stream, "redis_client", client)
    monkeypatch.setattr(stream, "_admin_ids", admins)
    monkeypatch.setattr(stream, "_down_until", 0.0)
    signal = _signal(recorded_by="system")
    await stream.publish_signal_update(_NO_DB, signal)
    assert sent == [(f"activity:{admin}", stream.signal_frame(signal))]


def test_case_frame_carries_ids_and_states_only() -> None:
    frame = stream.case_frame("c1", "resolved", "validation", "2026-11-21T08:00:00+00:00")
    assert frame == {
        "type": "ctem_case_update",
        "case_id": "c1",
        "state": "resolved",
        "event_kind": "validation",
        "at": "2026-11-21T08:00:00+00:00",
    }


def test_case_channels_are_personal_and_once_each() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert stream.case_channels([a, b, a]) == [f"activity:{a}", f"activity:{b}"]
    assert "activity:company" not in stream.case_channels([a])


@pytest.mark.asyncio
async def test_case_publish_sends_each_frame_to_each_user(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    class FakeRedis:
        async def publish(self, channel: str, payload: str) -> None:
            sent.append((channel, json.loads(payload)))

    async def client() -> FakeRedis:
        return FakeRedis()

    user = str(uuid.uuid4())

    async def users() -> list[str]:
        return [user]

    monkeypatch.setattr(stream, "redis_client", client)
    monkeypatch.setattr(stream, "_active_user_ids", users)
    monkeypatch.setattr(stream, "_down_until", 0.0)
    frame = stream.case_frame("c1", "new", "created", "2026-11-21T08:00:00+00:00")
    assert await stream.publish_case_updates([frame]) == 1
    assert sent == [(f"activity:{user}", frame)]


@pytest.mark.asyncio
async def test_case_publish_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken() -> list[str]:
        raise ConnectionError("db down")

    monkeypatch.setattr(stream, "_active_user_ids", broken)
    monkeypatch.setattr(stream, "_down_until", 0.0)
    frame = stream.case_frame("c1", "resolved", "validation", "x")
    assert await stream.publish_case_updates([frame]) == 0
    assert stream._down_until > 0
