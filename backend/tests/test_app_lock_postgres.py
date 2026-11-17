"""The companion's PIN lock against PostgreSQL, through the real routes.

Each request carries the session it comes from (its ``sid``), as the platform
tags it on the user: a phone's session once the companion opened on it, a
computer's never. A locked phone gets 423 from every gated route and from the
routes the Terms of Use gate leaves open (the profile, the Desktop handoff);
the computer is never affected and is where a forgotten PIN is reset. Five
wrong PINs end the phone's session. While the PIN applies, a push carries no
content and no buttons.

``integration``-marked; the rate limits are stubbed (they are not under test).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import app_lock as app_lock_api
from app.api.v1 import auth, desktop_login, mobile_devices
from app.core.database import get_db
from app.core.security import verify_password_async
from app.models.app_lock import AppLockSession
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import app_lock, mobile_push
from app.services.notification_feed import NotificationItem
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_mobile_devices_postgres import _subscription

# The companion is behind MOBILE_COMPANION: these run with it on.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("mobile_companion_enabled")]

PHONE = {"x-sid": "phone-session"}
COMPUTER = {"x-sid": "computer-session"}
PIN = "482915"


@asynccontextmanager
async def _client(session_factory, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(app_lock_api.router, prefix="/api/v1/app-lock")
    app.include_router(mobile_devices.router, prefix="/api/v1/mobile-devices")
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(desktop_login.router, prefix="/api/v1/desktop-login")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    def _authenticated(request: Request) -> User:
        # What ``resolve_session_jwt`` tags: the session the request comes from.
        sid = request.headers.get("x-sid")
        user.web_session = SimpleNamespace(claims={"sid": sid}) if sid else None
        return user

    # Only authentication is stubbed: the Terms of Use gate and the lock run.
    app.dependency_overrides[deps.get_current_user_ungated] = _authenticated
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def no_limits(monkeypatch):
    async def _allow(*_a, **_k):
        return None

    monkeypatch.setattr(app_lock_api, "enforce_rate_limit", _allow)
    monkeypatch.setattr(mobile_devices, "enforce_rate_limit", _allow)


@pytest.fixture
def policy(monkeypatch):
    """Switch the ``companion_pin_required`` policy for the test."""
    state = {"required": False}
    monkeypatch.setattr(app_lock, "pin_required", lambda: state["required"])
    return state


@asynccontextmanager
async def _world():
    async with scratch_database(
        "app_lock", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            db.add(owner)
            await db.commit()
        yield scratch, owner


def _code(response: httpx.Response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


async def test_a_locked_phone_answers_nothing_but_its_lock(no_limits, policy) -> None:
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        chosen = await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        assert chosen.status_code == 200, chosen.text
        assert (chosen.json()["applies"], chosen.json()["companion"], chosen.json()["locked"]) == (True, False, False)

        opened = (await client.post("/api/v1/app-lock/session", headers=PHONE)).json()
        assert (opened["companion"], opened["locked"], opened["attempts_left"]) == (True, True, 5)

        for method, path in [
            ("GET", "/api/v1/mobile-devices"),
            ("GET", "/api/v1/auth/me"),
            ("POST", f"/api/v1/desktop-login/{uuid.uuid4()}/complete"),
            ("POST", "/api/v1/auth/terms/accept"),
        ]:
            refused = await client.request(method, path, headers=PHONE, json={} if method == "POST" else None)
            assert (refused.status_code, _code(refused)) == (423, app_lock.LOCKED), path
        assert (await client.get("/api/v1/mobile-devices", headers=COMPUTER)).status_code == 200

        wrong = await client.post("/api/v1/app-lock/unlock", json={"pin": "999111"}, headers=PHONE)
        assert (wrong.status_code, wrong.json()["detail"]) == (403, {"code": "app_pin_wrong", "attempts_left": 4})
        unlocked = await client.post("/api/v1/app-lock/unlock", json={"pin": PIN}, headers=PHONE)
        assert (unlocked.status_code, unlocked.json()["locked"], unlocked.json()["attempts_left"]) == (200, False, 5)
        assert (await client.get("/api/v1/mobile-devices", headers=PHONE)).status_code == 200

        await client.post("/api/v1/app-lock/lock", headers=PHONE)
        assert (await client.get("/api/v1/mobile-devices", headers=PHONE)).status_code == 423

        # Opening the app again locks it again, unlocked or not.
        await client.post("/api/v1/app-lock/unlock", json={"pin": PIN}, headers=PHONE)
        reopened = (await client.post("/api/v1/app-lock/session", headers=PHONE)).json()
        assert reopened["locked"] is True


async def test_five_wrong_pins_end_the_phone_session_and_nothing_else(no_limits, policy) -> None:
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        await client.post("/api/v1/app-lock/session", headers=PHONE)
        answers = [
            await client.post("/api/v1/app-lock/unlock", json={"pin": "999111"}, headers=PHONE) for _ in range(5)
        ]
        assert [a.status_code for a in answers] == [403, 403, 403, 403, 401]
        assert [a.json()["detail"].get("attempts_left") for a in answers[:4]] == [4, 3, 2, 1]
        assert answers[4].json()["detail"] == {"code": app_lock.ENDED}

        # Ended for good: even the right PIN, even the lock's own status, is a sign-out now.
        right = await client.post("/api/v1/app-lock/unlock", json={"pin": PIN}, headers=PHONE)
        assert (right.status_code, _code(right)) == (401, app_lock.ENDED)
        gated = await client.get("/api/v1/mobile-devices", headers=PHONE)
        assert (gated.status_code, _code(gated)) == (401, app_lock.ENDED)
        assert (await client.get("/api/v1/mobile-devices", headers=COMPUTER)).status_code == 200

        async with scratch.sessions() as db:
            row = await db.get(AppLockSession, PHONE["x-sid"])
            assert (row.ended, row.failed_attempts) == (True, 5)
            ended = (
                (await db.execute(select(AuditLog).where(AuditLog.action == "session_ended"))).scalars().all()
            )
            assert len(ended) == 2
            assert {(e.entity_type, e.user_id) for e in ended} == {("app_lock", owner.id)}


async def test_the_policy_makes_the_phone_choose_a_pin_and_keeps_it_on(no_limits, policy) -> None:
    policy["required"] = True
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        opened = (await client.post("/api/v1/app-lock/session", headers=PHONE)).json()
        assert (opened["required"], opened["setup_required"], opened["has_pin"]) == (True, True, False)
        gated = await client.get("/api/v1/mobile-devices", headers=PHONE)
        assert (gated.status_code, _code(gated)) == (423, app_lock.SETUP_REQUIRED)
        # The computer is not a companion: no PIN asked there.
        assert (await client.get("/api/v1/mobile-devices", headers=COMPUTER)).status_code == 200

        weak = await client.put("/api/v1/app-lock/pin", json={"pin": "123456"}, headers=PHONE)
        assert (weak.status_code, weak.json()["detail"]) == (422, "Choose a PIN that is not a run of consecutive digits.")
        chosen = await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=PHONE)
        assert (chosen.status_code, chosen.json()["locked"], chosen.json()["setup_required"]) == (200, False, False)
        assert (await client.get("/api/v1/mobile-devices", headers=PHONE)).status_code == 200

        for headers in (PHONE, COMPUTER):
            off = await client.patch("/api/v1/app-lock", json={"enabled": False, "current_pin": PIN}, headers=headers)
            assert off.status_code == 409
        async with scratch.sessions() as db:
            assert (await app_lock.load_pin(db, owner.id)).enabled is True


async def test_a_phone_needs_the_current_pin_and_a_computer_resets_it(no_limits, policy) -> None:
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        await client.post("/api/v1/app-lock/session", headers=PHONE)
        await client.post("/api/v1/app-lock/unlock", json={"pin": PIN}, headers=PHONE)

        for body in ({"pin": "735190"}, {"pin": "735190", "current_pin": "999111"}):
            assert (await client.put("/api/v1/app-lock/pin", json=body, headers=PHONE)).status_code == 403
        assert (await client.patch("/api/v1/app-lock", json={"enabled": False}, headers=PHONE)).status_code == 403
        changed = await client.put("/api/v1/app-lock/pin", json={"pin": "735190", "current_pin": PIN}, headers=PHONE)
        assert changed.status_code == 200

        # Forgotten: the computer changes it without the current one.
        reset = await client.put("/api/v1/app-lock/pin", json={"pin": "246813"}, headers=COMPUTER)
        assert reset.status_code == 200
        await client.post("/api/v1/app-lock/lock", headers=PHONE)
        assert (
            await client.post("/api/v1/app-lock/unlock", json={"pin": "735190"}, headers=PHONE)
        ).status_code == 403
        assert (
            await client.post("/api/v1/app-lock/unlock", json={"pin": "246813"}, headers=PHONE)
        ).status_code == 200

        # Turned off from the computer: the phone answers even locked.
        off = await client.patch("/api/v1/app-lock", json={"enabled": False}, headers=COMPUTER)
        assert (off.status_code, off.json()["applies"]) == (200, False)
        await client.post("/api/v1/app-lock/lock", headers=PHONE)
        assert (await client.get("/api/v1/mobile-devices", headers=PHONE)).status_code == 200

        async with scratch.sessions() as db:
            summaries = (
                (await db.execute(select(AuditLog.summary).where(AuditLog.entity_type == "app_lock"))).scalars().all()
            )
            assert sorted(summaries) == sorted(
                ["Set the mobile companion PIN"] * 3 + ["Turned the mobile companion PIN off"]
            )


async def test_a_wrong_current_pin_counts_like_a_wrong_unlock(no_limits, policy) -> None:
    """Changing or turning off the PIN from the phone asks the current one: each wrong guess counts against the
    same five as an unlock, and the fifth ends the session - never unlimited guesses."""
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        await client.post("/api/v1/app-lock/session", headers=PHONE)
        answers = [
            await client.post("/api/v1/app-lock/unlock", json={"pin": "999111"}, headers=PHONE),
            await client.put("/api/v1/app-lock/pin", json={"pin": "735190", "current_pin": "999112"}, headers=PHONE),
            await client.patch("/api/v1/app-lock", json={"enabled": False, "current_pin": "999113"}, headers=PHONE),
            await client.put("/api/v1/app-lock/pin", json={"pin": "735190"}, headers=PHONE),
            await client.patch("/api/v1/app-lock", json={"enabled": False, "current_pin": "999114"}, headers=PHONE),
        ]
        assert [a.status_code for a in answers] == [403, 403, 403, 403, 401]
        assert [a.json()["detail"].get("attempts_left") for a in answers[:4]] == [4, 3, 2, 1]
        assert answers[4].json()["detail"] == {"code": app_lock.ENDED}
        # Ended: even the right PIN changes nothing from that phone.
        for call in (
            client.patch("/api/v1/app-lock", json={"enabled": False, "current_pin": PIN}, headers=PHONE),
            client.put("/api/v1/app-lock/pin", json={"pin": "735190", "current_pin": PIN}, headers=PHONE),
        ):
            refused = await call
            assert (refused.status_code, _code(refused)) == (401, app_lock.ENDED)
        async with scratch.sessions() as db:
            pin = await app_lock.load_pin(db, owner.id)
            row = await db.get(AppLockSession, PHONE["x-sid"])
            assert (pin.enabled, await verify_password_async(PIN, pin.pin_hash)) == (True, True)
            assert (row.ended, row.failed_attempts) == (True, 5)


async def test_the_current_pin_is_checked_under_the_unlock_budget(policy, monkeypatch) -> None:
    calls: list[tuple] = []

    async def _record(_request, scope, **kwargs):
        calls.append((scope, kwargs.get("bucket"), kwargs.get("fail_closed")))

    monkeypatch.setattr(app_lock_api, "enforce_rate_limit", _record)
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        await client.post("/api/v1/app-lock/session", headers=PHONE)
        calls.clear()
        await client.put("/api/v1/app-lock/pin", json={"pin": "735190", "current_pin": "999111"}, headers=PHONE)
        await client.patch("/api/v1/app-lock", json={"enabled": False, "current_pin": "999111"}, headers=PHONE)
        # A computer is asked no current PIN, so spends nothing of the phone's budget.
        await client.put("/api/v1/app-lock/pin", json={"pin": "735190"}, headers=COMPUTER)
    assert calls == [("app_lock_unlock", str(owner.id), True)] * 2


async def test_a_locked_phone_approves_no_mcp_client(no_limits, policy, monkeypatch) -> None:
    """The MCP consent page reads the session the browser holds: on a phone locked behind its PIN it grants
    nothing (the client's token would outlive the lock), and once unlocked it does."""
    from fastapi import HTTPException

    from app.api.v1 import mcp_oauth
    from app.core import security, sessions
    from app.models.oauth_client import OAuthClient

    codes: list[str] = []
    redirect = "https://localhost:8723/callback"

    async def _store(code, *_a, **_kw):
        codes.append(code)

    async def _audit(*_a, **_kw):
        return None

    async def _session(claims, _token, **_kw):
        return SimpleNamespace(claims=dict(claims))

    async def _json():
        return {
            "client_id": "mcp-client1", "redirect_uri": redirect, "state": "s", "code_challenge": "challenge",
            "code_challenge_method": "S256", "response_type": "code", "resource": "",
        }  # fmt: skip

    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        async with scratch.sessions() as db:
            db.add(OAuthClient(client_id="mcp-client1", client_name="VS Code", redirect_uris=[redirect]))
            await db.commit()
        await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        await client.post("/api/v1/app-lock/session", headers=PHONE)
        payload = {"sub": str(owner.id), "iat": 9_999_999_999, "sid": PHONE["x-sid"]}
        monkeypatch.setattr(security, "decode_access_token", lambda _t: payload)
        monkeypatch.setattr(sessions, "verify_session", _session)
        monkeypatch.setattr(mcp_oauth, "_store_auth_code", _store)
        monkeypatch.setattr(mcp_oauth, "log_audit", _audit)
        request = SimpleNamespace(headers={"Authorization": "Bearer tok"}, client=SimpleNamespace(host="127.0.0.1"), json=_json)

        async with scratch.sessions() as db:
            with pytest.raises(HTTPException) as refused:
                await mcp_oauth.authorize_session(request=request, db=db)
        assert (refused.value.status_code, refused.value.detail, codes) == (423, {"code": app_lock.LOCKED}, [])

        await client.post("/api/v1/app-lock/unlock", json={"pin": PIN}, headers=PHONE)
        async with scratch.sessions() as db:
            granted = await mcp_oauth.authorize_session(request=request, db=db)
        assert (granted.status_code, len(codes)) == (200, 1)


async def test_an_unlock_lasts_while_the_person_uses_the_app(policy) -> None:
    async with _world() as (scratch, owner):
        phone = SimpleNamespace(id=owner.id, web_session=SimpleNamespace(claims={"sid": "phone-session"}))
        start = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        async with scratch.sessions() as db:
            await app_lock.set_pin(db, phone, PIN)
            await app_lock.mark_companion_session(db, phone)
            assert await app_lock.unlock(db, phone, PIN, now=start) is True
            await db.commit()
        async with scratch.sessions() as db:
            await app_lock.extend(db, phone, now=start + timedelta(minutes=9))
            await db.commit()
        async with scratch.sessions() as db:
            await app_lock.enforce(db, phone, now=start + timedelta(minutes=18))
            with pytest.raises(Exception) as locked:
                await app_lock.enforce(db, phone, now=start + timedelta(minutes=19))
            assert locked.value.status_code == 423
            # Activity after the unlock ran out does not unlock again.
            await app_lock.extend(db, phone, now=start + timedelta(minutes=20))
            await db.commit()
            assert (await db.get(AppLockSession, "phone-session")).unlocked_until == start + timedelta(minutes=19)


async def test_while_the_pin_applies_a_push_carries_nothing(no_limits, policy, monkeypatch) -> None:
    sent: list[dict] = []

    async def _send(_subscription, payload, _keys, **_kwargs):
        sent.append(payload)
        return SimpleNamespace(outcome="sent")

    monkeypatch.setattr(mobile_push, "send_push", _send)
    asked: list[object] = []

    async def _actions(device):
        asked.append(device.id)
        return [{"action": "approve", "title": "Approve", "token": "t" * 43}]

    item = NotificationItem(
        id="run:1", kind="hitl", created_at="", route="/runs/1", title="Delete the tenant?", summary="Secret", run_id="1"
    )
    async with _world() as (scratch, owner), _client(scratch.sessions, owner) as client:
        linked = await client.post(
            "/api/v1/mobile-devices",
            json={"subscription": _subscription("https://fcm.googleapis.com/fcm/send/p"), "name": "Pixel"},
            headers=COMPUTER,
        )
        assert linked.status_code == 201, linked.text
        async with scratch.sessions() as db:
            await mobile_push.push_item_to_user(db, owner.id, item, actions_for=_actions)
        await client.put("/api/v1/app-lock/pin", json={"pin": PIN}, headers=COMPUTER)
        async with scratch.sessions() as db:
            await mobile_push.push_item_to_user(db, owner.id, item, actions_for=_actions)

    assert [p.get("kind") for p in sent] == ["hitl", "minimal"]
    assert sent[0]["title"] == "Delete the tenant?"
    assert [a["action"] for a in sent[0]["actions"]] == ["approve"]
    assert "actions" not in sent[1]
    assert "Delete" not in str(sent[1]) and "Secret" not in str(sent[1])
    assert len(asked) == 1
