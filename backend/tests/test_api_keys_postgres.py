"""Scoped personal API keys against real PostgreSQL.

The acceptance tests, driven through the real routers and the real
``deps.get_current_user`` with ``Authorization: Bearer fcp-...``: a key that
existed before scopes keeps working unchanged, a read-only key cannot write
through REST, a proxy-only key cannot call REST or MCP, an expired key is
refused on every surface, a key over its own limit gets a 429 while its owner's
other keys keep working; creating, changing and revoking a key is audited, the
maximum lifetime bounds new keys and only reports old ones, an expiry notice
goes out once per key and threshold. Redis is the in-memory fake of
``test_api_key_access.py``.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import agents, api_keys, llm_proxy, mcp_auth, security_audit
from app.core import rate_limit
from app.core.database import get_db
from app.models.activity import Activity
from app.models.agent import Agent
from app.models.api_key import UserApiKey
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import api_key_access as access
from app.services import api_key_notices
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_api_key_access import _FakeRedis

pytestmark = pytest.mark.integration


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _client():
        return redis

    monkeypatch.setattr(access, "redis_client", _client)
    monkeypatch.setattr(rate_limit, "redis_client", _client)
    return redis


@pytest.fixture
def every_surface(monkeypatch):
    monkeypatch.setattr(access, "available_surfaces", lambda: access.API_KEY_SURFACES)


@asynccontextmanager
async def _client(session_factory: async_sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    """The API key and agent routers, authenticated by the real dependency."""
    app = FastAPI()
    app.include_router(api_keys.router, prefix="/api/v1/api-keys")
    app.include_router(api_keys.admin_router, prefix="/api/v1/admin/api-keys")
    app.include_router(agents.router, prefix="/api/v1/agents")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _request(path: str, key: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"authorization", f"Bearer {key}".encode())],
            "query_string": b"",
        }
    )


async def _seed(db, *, role: str = "user", **key_fields) -> tuple[User, str, UserApiKey]:
    """A user and one key of theirs, written straight to the table (a key as it was before scopes, by default)."""
    user = User(email=f"{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Owner", role=role)
    db.add(user)
    await db.flush()
    raw, row = await _add_key(db, user, **key_fields)
    return user, raw, row


async def _add_key(db, user: User, **key_fields) -> tuple[str, UserApiKey]:
    raw = access.generate_api_key()
    row = UserApiKey(
        user_id=user.id,
        name=key_fields.pop("name", "Seeded"),
        key_prefix=access.display_prefix(raw),
        key_hash=await api_keys._hash_key(raw),
        **key_fields,
    )
    db.add(row)
    await db.flush()
    return raw, row


async def test_a_key_created_before_scopes_keeps_working_unchanged(fake_redis):
    async with _scratch("apikey_legacy") as scratch:
        async with scratch.sessions() as db:
            _user, raw, row = await _seed(db)
            await db.commit()
            assert (row.surfaces, row.read_only, row.agent_ids, row.expires_at) == (None, False, None, None)

        async with _client(scratch.sessions) as client:
            listed = await client.get("/api/v1/api-keys", headers=_bearer(raw))
            assert listed.status_code == 200, listed.text
            [shown] = listed.json()
            assert shown["full_scope"] and shown["all_surfaces"] and not shown["read_only"]
            created = await client.post("/api/v1/api-keys", json={"name": "Made by a key"}, headers=_bearer(raw))
            assert created.status_code == 201, created.text
            assert created.json()["full_scope"]

        async with scratch.sessions() as db:
            assert (await mcp_auth.authenticate_user(_request("/mcp/user", raw), db, "mcp/user")).email
            assert (await llm_proxy._authenticate_api_key(_request("/v1/chat/completions", raw), db)).email


async def test_a_read_only_key_cannot_write_through_rest(fake_redis):
    async with _scratch("apikey_readonly") as scratch:
        async with scratch.sessions() as db:
            _user, raw, _row = await _seed(db)
            await db.commit()

        async with _client(scratch.sessions) as client:
            created = await client.post(
                "/api/v1/api-keys", json={"name": "Reader", "read_only": True}, headers=_bearer(raw)
            )
            assert created.status_code == 201, created.text
            reader = created.json()["key"]
            reader_id = created.json()["id"]

            assert (await client.get("/api/v1/api-keys", headers=_bearer(reader))).status_code == 200
            for method, url, body in (
                ("POST", "/api/v1/api-keys", {"name": "Escalation"}),
                ("PATCH", f"/api/v1/api-keys/{reader_id}", {"read_only": False}),
                ("DELETE", f"/api/v1/api-keys/{reader_id}", None),
            ):
                refused = await client.request(method, url, json=body, headers=_bearer(reader))
                assert refused.status_code == 403, (method, refused.text)
                assert "read-only" in refused.json()["detail"]


async def test_a_proxy_only_key_cannot_call_rest_or_mcp(fake_redis, every_surface):
    async with _scratch("apikey_proxy_only") as scratch:
        async with scratch.sessions() as db:
            _user, raw, _row = await _seed(db, surfaces=["llm_proxy"])
            await db.commit()

        async with _client(scratch.sessions) as client:
            refused = await client.get("/api/v1/api-keys", headers=_bearer(raw))
            assert refused.status_code == 403, refused.text

        async with scratch.sessions() as db:
            with pytest.raises(HTTPException) as exc:
                await mcp_auth.authenticate_user(_request("/mcp/user", raw), db, "mcp/user")
            assert exc.value.status_code == 403
            assert (await llm_proxy._authenticate_api_key(_request("/v1/chat/completions", raw), db)).email


async def test_an_expired_key_is_refused_on_every_surface(fake_redis):
    async with _scratch("apikey_expired") as scratch:
        async with scratch.sessions() as db:
            _user, raw, _row = await _seed(db, expires_at=datetime.now(UTC) - timedelta(minutes=1))
            await db.commit()

        async with _client(scratch.sessions) as client:
            refused = await client.get("/api/v1/api-keys", headers=_bearer(raw))
            assert (refused.status_code, refused.json()["detail"]) == (401, access.EXPIRED_KEY_DETAIL)

        async with scratch.sessions() as db:
            with pytest.raises(HTTPException) as exc:
                await mcp_auth.authenticate_user(_request("/mcp/user", raw), db, "mcp/user")
            assert exc.value.status_code == 401
            assert "resource_metadata" in exc.value.headers["WWW-Authenticate"]
            with pytest.raises(HTTPException) as exc:
                await llm_proxy._authenticate_api_key(_request("/v1/chat/completions", raw), db)
            assert exc.value.status_code == 401


async def test_a_key_over_its_own_limit_gets_a_429_while_its_sibling_works(fake_redis):
    async with _scratch("apikey_limits") as scratch:
        async with scratch.sessions() as db:
            user, limited, _row = await _seed(db, rate_limit_per_minute=2)
            sibling, _sibling_row = await _add_key(db, user, name="Sibling")
            quota, _quota_row = await _add_key(db, user, name="Quota", quota_limit=1, quota_period="daily")
            await db.commit()

        async with _client(scratch.sessions) as client:
            for _ in range(2):
                assert (await client.get("/api/v1/api-keys", headers=_bearer(limited))).status_code == 200
            over = await client.get("/api/v1/api-keys", headers=_bearer(limited))
            assert over.status_code == 429 and over.headers["Retry-After"]
            assert (await client.get("/api/v1/api-keys", headers=_bearer(quota))).status_code == 200
            assert (await client.get("/api/v1/api-keys", headers=_bearer(quota))).status_code == 429
            for _ in range(4):
                assert (await client.get("/api/v1/api-keys", headers=_bearer(sibling))).status_code == 200


async def test_an_agent_restricted_key_reaches_its_agents_only(fake_redis):
    async with _scratch("apikey_agents") as scratch:
        async with scratch.sessions() as db:
            user, raw, _row = await _seed(db)
            mine, other = (
                Agent(name=name, slug=f"{name}-{uuid.uuid4().hex[:6]}", user_id=user.id) for name in ("mine", "other")
            )
            db.add_all([mine, other])
            await db.commit()
            mine_id, other_id = str(mine.id), str(other.id)

        async with _client(scratch.sessions) as client:
            unknown = await client.post(
                "/api/v1/api-keys", json={"name": "Bad", "agent_ids": [str(uuid.uuid4())]}, headers=_bearer(raw)
            )
            assert unknown.status_code == 422
            created = await client.post(
                "/api/v1/api-keys", json={"name": "One agent", "agent_ids": [mine_id]}, headers=_bearer(raw)
            )
            assert created.status_code == 201, created.text
            assert created.json()["agents"] == [{"id": mine_id, "name": "mine"}]
            restricted = created.json()["key"]

            assert (await client.get(f"/api/v1/agents/{mine_id}", headers=_bearer(restricted))).status_code == 200
            for url in (f"/api/v1/agents/{other_id}", "/api/v1/agents", "/api/v1/api-keys"):
                refused = await client.get(url, headers=_bearer(restricted))
                assert refused.status_code == 403, url
                assert refused.json()["detail"] == access.AGENT_RESTRICTED_DETAIL


async def test_a_key_cannot_mint_a_wider_key(fake_redis):
    async with _scratch("apikey_widen") as scratch:
        async with scratch.sessions() as db:
            _user, raw, _row = await _seed(db, surfaces=["rest"])
            await db.commit()

        async with _client(scratch.sessions) as client:
            wider = await client.post("/api/v1/api-keys", json={"name": "Wider"}, headers=_bearer(raw))
            assert wider.status_code == 403
            same = await client.post(
                "/api/v1/api-keys", json={"name": "Same", "surfaces": ["rest"]}, headers=_bearer(raw)
            )
            assert same.status_code == 201, same.text


async def test_the_maximum_lifetime_bounds_new_keys_and_reports_old_ones(fake_redis, monkeypatch):
    monkeypatch.setattr(access, "max_lifetime_days", lambda: 30)
    async with _scratch("apikey_lifetime") as scratch:
        async with scratch.sessions() as db:
            _user, legacy, row = await _seed(db, name="Forever", created_at=datetime.now(UTC) - timedelta(days=400))
            legacy_id = str(row.id)
            await db.commit()

        async with _client(scratch.sessions) as client:
            # The old key is never cut off on its own.
            assert (await client.get("/api/v1/api-keys", headers=_bearer(legacy))).status_code == 200
            created = await client.post("/api/v1/api-keys", json={"name": "Bounded"}, headers=_bearer(legacy))
            assert created.status_code == 201, created.text
            expires_at = datetime.fromisoformat(created.json()["expires_at"])
            assert timedelta(days=29) < expires_at - datetime.now(UTC) <= timedelta(days=30)
            too_long = await client.post(
                "/api/v1/api-keys",
                json={"name": "Too long", "expires_at": (datetime.now(UTC) + timedelta(days=60)).isoformat()},
                headers=_bearer(legacy),
            )
            assert too_long.status_code == 422
            policy = await client.get("/api/v1/api-keys/policy", headers=_bearer(legacy))
            assert policy.json()["max_lifetime_days"] == 30

        async with scratch.sessions() as db:
            findings: list[security_audit.AuditFinding] = []
            await security_audit._check_api_keys(db, findings)
            by_id = {finding.id: finding for finding in findings}
            assert by_id["authentication.api_keys_beyond_max_lifetime"].severity == "warning"
            assert "Forever" in by_id["authentication.api_keys_beyond_max_lifetime"].description
            assert "Bounded" not in by_id["authentication.api_keys_beyond_max_lifetime"].description
            assert "authentication.api_keys_full_scope" in by_id

        # Its owner re-dates it within the bound: the report lets it go, however old the key is.
        async with _client(scratch.sessions) as client:
            redated = await client.patch(
                f"/api/v1/api-keys/{legacy_id}",
                json={"expires_at": (datetime.now(UTC) + timedelta(days=20)).isoformat()},
                headers=_bearer(legacy),
            )
            assert redated.status_code == 200, redated.text
            assert redated.json()["beyond_max_lifetime"] is False
        async with scratch.sessions() as db:
            findings = []
            await security_audit._check_api_keys(db, findings)
            assert "authentication.api_keys_beyond_max_lifetime" not in {finding.id for finding in findings}


async def test_changing_and_revoking_a_key_is_audited_and_revocation_bites(fake_redis):
    async with _scratch("apikey_revoke") as scratch:
        async with scratch.sessions() as db:
            owner, raw, row = await _seed(db, name="Owner key")
            _admin, admin_key, _admin_row = await _seed(db, role="admin", name="Admin key")
            await db.commit()
            key_id, owner_id = str(row.id), owner.id

        async with _client(scratch.sessions) as client:
            changed = await client.patch(
                f"/api/v1/api-keys/{key_id}",
                json={"surfaces": ["rest"], "quota_limit": 100},
                headers=_bearer(raw),
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["surfaces"] == ["rest"] and changed.json()["quota_period"] == "monthly"

            refused = await client.get("/api/v1/admin/api-keys", headers=_bearer(raw))
            assert refused.status_code == 403
            listed = await client.get("/api/v1/admin/api-keys?status=active", headers=_bearer(admin_key))
            assert listed.status_code == 200, listed.text
            assert {item["name"] for item in listed.json()["items"]} == {"Owner key", "Admin key"}
            assert listed.json()["total"] == 2
            mine = await client.get(f"/api/v1/admin/api-keys?owner_id={owner_id}", headers=_bearer(admin_key))
            assert [item["name"] for item in mine.json()["items"]] == ["Owner key"]
            malformed = await client.get("/api/v1/admin/api-keys?owner_id=nope", headers=_bearer(admin_key))
            assert malformed.status_code == 422

            revoked = await client.post(f"/api/v1/admin/api-keys/{key_id}/revoke", headers=_bearer(admin_key))
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["is_active"] is False and revoked.json()["revoked_at"]
            assert (await client.get("/api/v1/api-keys", headers=_bearer(raw))).status_code == 401
            listed = await client.get("/api/v1/admin/api-keys?status=revoked", headers=_bearer(admin_key))
            assert [item["name"] for item in listed.json()["items"]] == ["Owner key"]

        async with scratch.sessions() as db:
            entries = (
                await db.execute(
                    select(AuditLog.action, AuditLog.details).where(AuditLog.entity_id == uuid.UUID(key_id))
                )
            ).all()
            actions = {action: details for action, details in entries}
            assert set(actions) == {"update", "revoke"}
            assert set(actions["update"]["changes"]) == {"surfaces", "quota_limit", "quota_period"}
            notices = (await db.execute(select(Activity).where(Activity.user_id == owner_id))).scalars().all()
            assert [notice.extra_data["notification_source"] for notice in notices] == ["api_key"]


async def test_two_revocations_of_one_key_audit_it_and_tell_its_owner_once(fake_redis):
    async with _scratch("apikey_revoke_race") as scratch:
        async with scratch.sessions() as db:
            owner, _raw, row = await _seed(db, name="Owner key")
            _admin, admin_key, _admin_row = await _seed(db, role="admin", name="Admin key")
            await db.commit()
            key_id, owner_id = str(row.id), owner.id

        async with _client(scratch.sessions) as client:
            url = f"/api/v1/admin/api-keys/{key_id}/revoke"
            first, second = await asyncio.gather(
                client.post(url, headers=_bearer(admin_key)),
                client.post(url, headers=_bearer(admin_key)),
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["is_active"] is False and second.json()["is_active"] is False

        async with scratch.sessions() as db:
            revocations = (
                await db.execute(
                    select(AuditLog.id).where(AuditLog.entity_id == uuid.UUID(key_id), AuditLog.action == "revoke")
                )
            ).all()
            assert len(revocations) == 1
            notices = (await db.execute(select(Activity).where(Activity.user_id == owner_id))).scalars().all()
            assert len(notices) == 1


async def test_the_admin_list_filters_on_owners_and_sums_every_matching_key(fake_redis):
    async with _scratch("apikey_admin_summary") as scratch:
        now = datetime.now(UTC)
        async with scratch.sessions() as db:
            first, _raw, _busy = await _seed(db, name="Busy", request_count=40, last_used_at=now - timedelta(days=2))
            await _add_key(db, first, name="Recent", request_count=3, last_used_at=now - timedelta(hours=1))
            await _add_key(db, first, name="Expired", request_count=1, expires_at=now - timedelta(days=1))
            second, _raw, _other = await _seed(db, name="Other owner", request_count=7)
            gone, _raw, _gone_key = await _seed(db, name="Deactivated owner")
            gone.is_active = False
            _admin, admin_key, _admin_row = await _seed(
                db, role="admin", name="Admin key", expires_at=now + timedelta(days=30)
            )
            await db.commit()
            first_id, second_id, gone_id = first.id, second.id, gone.id

        async with _client(scratch.sessions) as client:
            admin = _bearer(admin_key)
            both = await client.get(
                f"/api/v1/admin/api-keys?owner_id={first_id}&owner_id={second_id}&limit=1&with_summary=true",
                headers=admin,
            )
            assert both.status_code == 200, both.text
            page = both.json()
            assert page["total"] == 4 and len(page["items"]) == 1
            # The summary covers every matching key, not the one-key page.
            assert page["summary"] == {
                "usable": 3,
                "request_count": 51,
                "last_used_at": page["summary"]["last_used_at"],
                "last_used_key_name": "Recent",
                "most_active_key_name": "Busy",
                "most_active_request_count": 40,
            }
            plain = await client.get(f"/api/v1/admin/api-keys?owner_id={first_id}", headers=admin)
            assert plain.json()["total"] == 3 and plain.json()["summary"] is None

            # The exposure filters list what the Security Audit counts: the keys of active accounts.
            no_expiry = await client.get("/api/v1/admin/api-keys?status=active&issue=no_expiry", headers=admin)
            assert {item["name"] for item in no_expiry.json()["items"]} == {"Busy", "Recent", "Other owner"}
            full = await client.get("/api/v1/admin/api-keys?status=active&issue=full_scope", headers=admin)
            assert {item["name"] for item in full.json()["items"]} == {"Busy", "Recent", "Other owner", "Admin key"}
            dormant = await client.get(f"/api/v1/admin/api-keys?owner_id={gone_id}", headers=admin)
            assert [item["name"] for item in dormant.json()["items"]] == ["Deactivated owner"]

            too_many = "&".join(f"owner_id={uuid.uuid4()}" for _ in range(api_keys.MAX_ADMIN_OWNER_FILTER + 1))
            assert (await client.get(f"/api/v1/admin/api-keys?{too_many}", headers=admin)).status_code == 422


async def test_an_expiry_notice_goes_out_once_per_key_and_threshold(fake_redis):
    async with _scratch("apikey_notices") as scratch:
        now = datetime.now(UTC)
        async with scratch.sessions() as db:
            user, _raw, soon = await _seed(db, name="Soon", expires_at=now + timedelta(days=3))
            _raw, tomorrow = await _add_key(db, user, name="Tomorrow", expires_at=now + timedelta(hours=20))
            _raw, later = await _add_key(db, user, name="Later", expires_at=now + timedelta(days=30))
            await db.commit()

        async with scratch.sessions() as db:
            assert await api_key_notices.notify_expiring_api_keys(db, now=now) == 2
        async with scratch.sessions() as db:
            assert await api_key_notices.notify_expiring_api_keys(db, now=now + timedelta(hours=1)) == 0
        async with scratch.sessions() as db:
            # Two days on, the 3-day key enters its last day: its second notice.
            assert await api_key_notices.notify_expiring_api_keys(db, now=now + timedelta(days=2, hours=1)) == 1
        async with scratch.sessions() as db:
            notified = dict(
                (await db.execute(select(UserApiKey.name, UserApiKey.expiry_notified_days))).all()
            )
            assert notified == {"Soon": 1, "Tomorrow": 1, "Later": None}
            titles = sorted(
                (await db.execute(select(Activity.title).where(Activity.user_id == user.id))).scalars().all()
            )
            assert len(titles) == 3 and all("expires within" in title for title in titles)
        del soon, tomorrow, later


async def test_keys_already_told_or_of_a_disabled_account_never_fill_the_notice_batch(fake_redis, monkeypatch):
    monkeypatch.setattr(api_key_notices, "_SCAN_LIMIT", 1)
    async with _scratch("apikey_notice_batch") as scratch:
        now = datetime.now(UTC)
        async with scratch.sessions() as db:
            dormant_owner, _raw, _dormant = await _seed(db, name="Dormant", expires_at=now + timedelta(hours=12))
            dormant_owner.is_active = False
            owner, _raw, _told = await _seed(
                db, name="Told", expires_at=now + timedelta(days=2), expiry_notified_days=7
            )
            _raw, _untold = await _add_key(db, owner, name="Untold", expires_at=now + timedelta(days=5))
            await db.commit()
            dormant_owner_id = dormant_owner.id

        async with scratch.sessions() as db:
            assert await api_key_notices.notify_expiring_api_keys(db, now=now) == 1
        async with scratch.sessions() as db:
            notified = dict((await db.execute(select(UserApiKey.name, UserApiKey.expiry_notified_days))).all())
            # Nothing is recorded for the disabled account: it was never told.
            assert notified == {"Dormant": None, "Told": 7, "Untold": 7}
            (await db.get(User, dormant_owner_id)).is_active = True
            await db.commit()

        async with scratch.sessions() as db:
            assert await api_key_notices.notify_expiring_api_keys(db, now=now + timedelta(minutes=5)) == 1
        async with scratch.sessions() as db:
            notified = dict((await db.execute(select(UserApiKey.name, UserApiKey.expiry_notified_days))).all())
            assert notified == {"Dormant": 1, "Told": 7, "Untold": 7}
