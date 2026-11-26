"""Linked phones against PostgreSQL: ownership, shared read state, the buttons.

A phone is its owner's: anyone else gets the 404 of an unknown id on every
route; an administrator may list and unlink (offboarding). Read state is the
server's, so two sessions agree. A notification's Approve button decides the
run exactly once through the web app's own route, and is refused once spent,
once the phone is unlinked, or once the pause changed. A notification is
pushed only after the transaction that wrote it commits.

``integration``-marked; Redis is an in-memory stand-in, the run's resume is
stubbed (the engine is not under test here).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import mobile_devices, notifications
from app.core.database import get_db
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.mobile_device import MobileDevice
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_mobile_push import _FakeRedis

# The companion is behind MOBILE_COMPANION: these run with it on.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("mobile_companion_enabled")]


def _subscription(endpoint: str) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    public = key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return {"endpoint": endpoint, "keys": {"p256dh": b64(public), "auth": b64(b"0123456789abcdef")}}


@asynccontextmanager
async def _client(session_factory, user: User | None) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(mobile_devices.router, prefix="/api/v1/mobile-devices")
    app.include_router(mobile_devices.actions_router, prefix="/api/v1/mobile")
    app.include_router(notifications.router, prefix="/api/v1/notifications")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    if user is not None:
        app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def no_limits(monkeypatch):
    fake = _FakeRedis()

    async def _redis():
        return fake

    async def _allow(*_a, **_k):
        return None

    monkeypatch.setattr("app.core.redis.redis_client", _redis)
    monkeypatch.setattr(mobile_devices, "enforce_rate_limit", _allow)
    return fake


@asynccontextmanager
async def _world():
    async with scratch_database(
        "mobile_devices", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            other = User(email="other@example.com", hashed_password="x", full_name="Other", role="user")
            admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
            db.add_all([owner, other, admin])
            await db.commit()
        yield scratch, owner, other, admin


async def test_a_phone_is_its_owners_on_every_route(no_limits) -> None:
    async with _world() as (scratch, owner, other, admin):
        async with _client(scratch.sessions, owner) as client:
            linked = await client.post(
                "/api/v1/mobile-devices",
                json={
                    "subscription": _subscription("https://fcm.googleapis.com/fcm/send/a"),
                    "name": "Pixel",
                    "platform": "android",
                },
            )
            assert linked.status_code == 201, linked.text
            phone = linked.json()
            assert (phone["name"], phone["push_enabled"], phone["current"]) == ("Pixel", True, True)
            refused = await client.post(
                "/api/v1/mobile-devices",
                json={"subscription": _subscription("https://evil.example/push"), "name": "Evil"},
            )
            assert refused.status_code == 422
            assert "evil" not in refused.text.lower()
            # A port urlsplit cannot read is the same 422, never a 500.
            for endpoint in ("https://fcm.googleapis.com:99999/x", "https://[fcm.googleapis.com/x"):
                malformed = await client.post(
                    "/api/v1/mobile-devices", json={"subscription": _subscription(endpoint), "name": "Odd"}
                )
                assert malformed.status_code == 422, endpoint
        url = f"/api/v1/mobile-devices/{phone['id']}"
        async with _client(scratch.sessions, other) as client:
            assert (await client.get("/api/v1/mobile-devices")).json() == []
            assert (await client.patch(url, json={"name": "Mine"})).status_code == 404
            assert (await client.post(f"{url}/test")).status_code == 404
            assert (await client.delete(url)).status_code == 404
            assert (await client.get(f"/api/v1/mobile-devices?user_id={owner.id}")).status_code == 404
        async with _client(scratch.sessions, admin) as client:
            listed = await client.get(f"/api/v1/mobile-devices?user_id={owner.id}")
            assert [p["id"] for p in listed.json()] == [phone["id"]]
            assert (await client.patch(url, json={"name": "Admin"})).status_code == 404
            assert (await client.delete(url)).status_code == 204
        async with scratch.sessions() as db:
            assert (await db.execute(select(MobileDevice))).scalars().all() == []


async def test_a_phone_linked_again_by_someone_else_moves_to_them(no_limits) -> None:
    async with _world() as (scratch, owner, other, _admin):
        subscription = _subscription("https://web.push.apple.com/same")
        async with _client(scratch.sessions, owner) as client:
            first = (await client.post("/api/v1/mobile-devices", json={"subscription": subscription})).json()
            muted = await client.patch(f"/api/v1/mobile-devices/{first['id']}", json={"muted_kinds": ["reminder"]})
            assert muted.json()["muted_kinds"] == ["reminder"]
            assert (
                await client.patch(f"/api/v1/mobile-devices/{first['id']}", json={"muted_kinds": ["nope"]})
            ).status_code == 422
        async with _client(scratch.sessions, other) as client:
            second = (await client.post("/api/v1/mobile-devices", json={"subscription": subscription})).json()
        assert second["id"] == first["id"]
        assert second["muted_kinds"] == []
        async with scratch.sessions() as db:
            [row] = (await db.execute(select(MobileDevice))).scalars().all()
            assert row.user_id == other.id


async def test_read_state_is_shared_by_every_session(no_limits) -> None:
    async with _world() as (scratch, owner, _other, _admin):
        async with scratch.sessions() as db:
            db.add_all(
                [
                    Activity(
                        user_id=owner.id,
                        activity_type="notification",
                        title="Reminder",
                        extra_data={"notification_source": "work_item"},
                    ),
                    Activity(
                        user_id=owner.id,
                        activity_type="notification",
                        title="Agent says",
                        extra_data={"notification_source": "agent", "path": "/dashboards"},
                    ),
                    Activity(user_id=owner.id, activity_type="notification", title="Run row", extra_data={}),
                ]
            )
            await db.commit()
        async with _client(scratch.sessions, owner) as phone, _client(scratch.sessions, owner) as computer:
            feed = (await phone.get("/api/v1/notifications")).json()
            assert sorted((i["kind"], i["route"], i["unread"]) for i in feed["items"]) == [
                ("agent_message", "/dashboards", True),
                ("reminder", "/operations", True),
            ]
            assert feed["unread_count"] == 2
            reminder = next(i for i in feed["items"] if i["kind"] == "reminder")
            assert (await phone.post(f"/api/v1/notifications/{reminder['id']}/dismiss")).status_code == 204
            assert (await computer.get("/api/v1/notifications")).json()["unread_count"] == 1
            read = await computer.post("/api/v1/notifications/read", json={})
            assert read.json()["unread_count"] == 0
            # A browser's old marker (earlier) never unreads anything.
            older = await phone.post("/api/v1/notifications/read", json={"read_at": "2020-01-01T00:00:00Z"})
            assert older.json()["unread_count"] == 0
            assert (
                await phone.post("/api/v1/notifications/read", json={"dismissed_ids": ["<script>"]})
            ).status_code == 422


async def test_a_waiting_work_item_entry_stays_unread_through_the_dismiss_route(
    no_limits: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#901 accepts the feed's ``work_item:<uuid>`` id on dismiss; #991 keeps it unread while it waits."""
    from app.services import work_item

    item_id = str(uuid.uuid4())

    async def _waiting(*, user_id: str, limit: int, offset: int = 0) -> list[dict[str, str]]:
        return [{"id": item_id, "title": "Pick", "question": "A or B?", "since": "2026-11-21T08:00:00+00:00"}]

    monkeypatch.setattr(work_item, "list_work_items_waiting_for_person", _waiting)
    async with _world() as (scratch, owner, _other, _admin), _client(scratch.sessions, owner) as client:
        feed = (await client.get("/api/v1/notifications")).json()
        entry = next(i for i in feed["items"] if i["kind"] == "decision" and i["source"] == "work_item")
        assert entry["id"] == f"work_item:{item_id}"
        assert entry["unread"] is True
        assert (await client.post(f"/api/v1/notifications/{entry['id']}/dismiss")).status_code == 204
        after = (await client.get("/api/v1/notifications")).json()
        again = next(i for i in after["items"] if i["id"] == entry["id"])
        assert again["unread"] is True
        assert again["awaiting_answer"] is True
        imported = await client.post("/api/v1/notifications/read", json={"dismissed_ids": [entry["id"]]})
        assert imported.status_code == 200
        assert (await client.post("/api/v1/notifications/work_item:nope/dismiss")).status_code == 404


async def test_mark_all_read_never_reads_a_waiting_decision(
    no_limits: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#991: neither a dismiss nor "Mark all read" reads a decision that still waits for an answer."""
    from app.services import work_item

    item_id = str(uuid.uuid4())

    async def _waiting(*, user_id: str, limit: int, offset: int = 0) -> list[dict[str, str]]:
        return [{"id": item_id, "title": "Pick", "question": "A or B?", "since": "2026-11-21T08:00:00+00:00"}]

    monkeypatch.setattr(work_item, "list_work_items_waiting_for_person", _waiting)
    async with _world() as (scratch, owner, _other, _admin):
        async with scratch.sessions() as db:
            await _paused_run(db, owner)
        async with _client(scratch.sessions, owner) as client:
            feed = (await client.get("/api/v1/notifications")).json()
            pinned = [i for i in feed["items"] if i["kind"] == "decision"]
            assert {i["source"] for i in pinned} == {"run", "work_item"}
            assert all(i["unread"] for i in pinned)
            for entry in pinned:
                assert (await client.post(f"/api/v1/notifications/{entry['id']}/dismiss")).status_code == 204
            read = (await client.post("/api/v1/notifications/read", json={})).json()
            ids = {i["id"] for i in pinned}
            assert all(i["unread"] is True for i in read["items"] if i["id"] in ids)
            assert read["unread_count"] == len(ids)
            after = (await client.get("/api/v1/notifications")).json()
            assert all(i["unread"] is True for i in after["items"] if i["id"] in ids)
            assert after["unread_count"] == len(ids)


async def _paused_run(db, owner: User) -> AssignmentRun:
    agent = Agent(id=uuid.uuid4(), name="agent", user_id=owner.id)
    db.add(agent)
    await db.flush()
    assignment = Assignment(
        id=uuid.uuid4(), agent_id=agent.id, name="a", trigger_type="manual", prompt="p", user_id=owner.id
    )
    db.add(assignment)
    await db.flush()
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=owner.id,
        status="awaiting_human",
        human_input_question="Approval required before running: isolate_host",
        pending_approvals=[{"tool_call_id": "call-1", "tool_name": "isolate_host", "input": {}}],
    )
    db.add(run)
    await db.commit()
    return run


async def test_a_notification_button_decides_once_through_the_web_apps_route(no_limits) -> None:
    from app.services.push_action_tokens import approval_actions

    async with _world() as (scratch, owner, _other, _admin):
        async with scratch.sessions() as db:
            run = await _paused_run(db, owner)
            device = MobileDevice(
                user_id=owner.id, name="Pixel", platform="android", push_subscription="x", endpoint_hash="h" * 64
            )
            db.add(device)
            await db.commit()
            run_id, device_id = run.id, device.id
        actions = await approval_actions(
            run_id=run_id, user_id=owner.id, device_id=device_id, pending=[{"tool_call_id": "call-1"}]
        )
        approve, reject = (a["token"] for a in actions)
        resumed: list = []

        async def _resume(db, run):
            resumed.append(run.id)
            run.status = "running"
            await db.commit()

        with mock.patch("app.services.assignment_engine.resume_assignment_run", _resume):
            async with _client(scratch.sessions, None) as client:
                first, second = await asyncio.gather(
                    client.post("/api/v1/mobile/push-actions", json={"token": approve}),
                    client.post("/api/v1/mobile/push-actions", json={"token": approve}),
                )
                assert sorted([first.status_code, second.status_code]) == [200, 403]
                # Deciding voided the other button of the same notification.
                late = await client.post("/api/v1/mobile/push-actions", json={"token": reject})
                assert late.status_code == 403
                assert late.json()["detail"] == mobile_devices.ACTION_REFUSED
                assert (await client.post("/api/v1/mobile/push-actions", json={"token": "x" * 40})).status_code == 403
        assert resumed == [run_id]
        async with scratch.sessions() as db:
            stored = await db.get(AssignmentRun, run_id)
            assert {k: v["decision"] for k, v in (stored.approval_decisions or {}).items()} == {"call-1": "approve"}


async def test_an_unlinked_phone_or_a_changed_pause_refuses_its_buttons(no_limits) -> None:
    from app.services.push_action_tokens import approval_actions

    async with _world() as (scratch, owner, _other, _admin):
        async with scratch.sessions() as db:
            run = await _paused_run(db, owner)
            device = MobileDevice(
                user_id=owner.id, name="Pixel", platform="android", push_subscription="x", endpoint_hash="h" * 64
            )
            db.add(device)
            await db.commit()
            run_id, device_id = run.id, device.id
        stale = await approval_actions(
            run_id=run_id, user_id=owner.id, device_id=device_id, pending=[{"tool_call_id": "old-call"}]
        )
        live = await approval_actions(
            run_id=run_id, user_id=owner.id, device_id=device_id, pending=[{"tool_call_id": "call-1"}]
        )
        async with _client(scratch.sessions, None) as client:
            # The pause changed since the push: its decision covers other calls.
            assert (
                await client.post("/api/v1/mobile/push-actions", json={"token": stale[0]["token"]})
            ).status_code == 403
        async with _client(scratch.sessions, owner) as client:
            assert (await client.delete(f"/api/v1/mobile-devices/{device_id}")).status_code == 204
        async with _client(scratch.sessions, None) as client:
            assert (
                await client.post("/api/v1/mobile/push-actions", json={"token": live[0]["token"]})
            ).status_code == 403
        async with scratch.sessions() as db:
            assert (await db.get(AssignmentRun, run_id)).status == "awaiting_human"


async def test_a_notification_is_pushed_only_once_its_transaction_commits(monkeypatch) -> None:
    from app.services import mobile_push
    from app.services.notify import notify_web_user

    queued: list[dict] = []

    async def _enqueue(job):
        queued.append(job)

    async def _no_sse(*_a, **_k):
        return None

    monkeypatch.setattr(mobile_push, "_enqueue", _enqueue)
    monkeypatch.setattr("app.services.notify._notify_web", _no_sse)
    async with _world() as (scratch, owner, _other, _admin):
        async with scratch.sessions() as db:
            await notify_web_user(db, str(owner.id), title="Rolled back", extra_data={"notification_source": "agent"})
            await db.rollback()
            await notify_web_user(db, str(owner.id), title="Kept", extra_data={"notification_source": "agent"})
            await db.commit()
        await asyncio.sleep(0)
        async with scratch.sessions() as db:
            kept = (await db.execute(select(Activity).where(Activity.title == "Kept"))).scalar_one()
        assert queued == [{"user_id": str(owner.id), "ref_kind": "activity", "ref_id": str(kept.id)}]
