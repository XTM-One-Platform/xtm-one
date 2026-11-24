"""Linked phones, unit level: the feed, the push itself, the action tokens, the tool.

No database and no Redis: the feed is presented from plain objects, the push is
sent to an ``httpx.MockTransport``, the tokens live in an in-memory Redis
stand-in. Routes, ownership and the decision path run against PostgreSQL in
``test_mobile_devices_postgres.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services import mobile_push
from app.services.mobile_push import (
    PushEndpointError,
    Subscription,
    VapidKeys,
    _generate_vapid_keys,
    device_wants,
    encrypt_payload,
    in_quiet_hours,
    push_endpoint_problem,
    push_payload,
    send_push,
    vapid_authorization,
)
from app.services.notification_feed import (
    NotificationItem,
    is_unread,
    merge_items,
    present_activity,
    present_run,
    summary_preview,
)

# The companion is behind MOBILE_COMPANION: these run with it on.
pytestmark = pytest.mark.usefixtures("mobile_companion_enabled")

# ── The notification feed: one presentation, one route rule ──────────────────

RUN_ID = "11111111-1111-4111-8111-111111111111"
UUID_A = "22222222-2222-4222-8222-222222222222"
UUID_B = "33333333-3333-4333-8333-333333333333"


def _activity(activity_type="notification", **metadata):
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        activity_type=activity_type,
        agent_name=None,
        title="Title",
        summary="Summary",
        extra_data=metadata,
        created_at="2026-10-20T08:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("activity", "kind", "route"),
    [
        (_activity("assignment_auth_failure", assignment_id=UUID_A), "auth_failure", f"/agents/assignments/{UUID_A}"),
        (_activity("assignment_auth_quarantine"), "auth_failure", "/integrations"),
        (
            _activity("assignment_stale_reference_pause", assignment_id=UUID_A),
            "stale_reference",
            f"/agents/assignments/{UUID_A}",
        ),
        (_activity("assignment_continuation_refused"), "continuation_refused", "/assignments"),
        (
            _activity(notification_source="llm_analytics_refresh", dashboard_path="/observability?range=7d"),
            "analytics_refresh",
            "/observability?range=7d",
        ),
        (
            _activity(notification_source="llm_analytics_refresh", dashboard_path="https://evil.example"),
            "analytics_refresh",
            "/observability",
        ),
        (
            _activity(notification_source="flow_hitl", flow_id=UUID_A, flow_run_id=UUID_B),
            "decision",
            f"/flows/library/{UUID_A}?run={UUID_B}",
        ),
        (_activity(notification_source="flow", flow_id="not-a-uuid"), "flow", "/flows"),
        (
            _activity(notification_source="security_audit", audit_path="/settings/security/audit?finding=x"),
            "security_audit",
            "/settings/security/audit?finding=x",
        ),
        (_activity(notification_source="security_audit", audit_path="/agents"), "security_audit", "/settings/security"),
        (_activity(notification_source="api_key"), "api_key", "/profile/api-keys"),
        (
            _activity(notification_source="agent_account", account_id=UUID_A, notification_kind="sent"),
            "agent_account",
            f"/profile/agent-accounts/{UUID_A}/replies",
        ),
        (
            _activity(notification_source="agent_account", account_id=UUID_A),
            "agent_account",
            f"/profile/agent-accounts/{UUID_A}/replies?status=pending",
        ),
        (_activity(notification_source="agent_account", account_id="../x"), "agent_account", "/profile/agent-accounts"),
        (
            _activity(notification_source="work_item", run_id=UUID_B, conversation_id=UUID_A),
            "reminder",
            f"/operations/executions/{UUID_B}",
        ),
        (
            _activity(notification_source="work_item", run_id=UUID_B, conversation_id=UUID_A, escalated=True),
            "reminder",
            f"/operations/executions/{UUID_B}",
        ),
        (_activity(notification_source="work_item", conversation_id=UUID_A), "reminder", f"/chat?c={UUID_A}"),
        (
            _activity(notification_source="work_item", run_id="../x", conversation_id=UUID_A),
            "reminder",
            f"/chat?c={UUID_A}",
        ),
        (_activity(notification_source="work_item", run_id=None, conversation_id=None), "reminder", "/operations"),
        (_activity(notification_source="work_item"), "reminder", "/operations"),
        (_activity(notification_source="agent", path="/dashboards/x"), "agent_message", "/dashboards/x"),
        (_activity(notification_source="agent", path="//evil.example"), "agent_message", "/"),
        (
            _activity(notification_source="run_action_request", run_id=UUID_A),
            "run_action_request",
            f"/operations/executions/{UUID_A}",
        ),
        (_activity(notification_source="run_action_request", run_id="../x"), "run_action_request", "/operations"),
    ],
)
def test_every_kind_has_one_route_inside_the_app(activity, kind, route):
    item = present_activity(activity, flows_enabled=True)
    assert (item.kind, item.route) == (kind, route)


def test_rows_the_bell_never_showed_stay_out():
    assert present_activity(_activity(), flows_enabled=True) is None  # a run's own row: the run is shown
    assert present_activity(_activity("assignment_run"), flows_enabled=True) is None
    # Behind the flow builder's flag, a link nobody can open is not shown.
    assert present_activity(_activity(notification_source="flow"), flows_enabled=False) is None


def _run(status="completed", question=None, summary="The weekly brief found 3 new critical exposures."):
    return SimpleNamespace(
        id=RUN_ID,
        status=status,
        created_at="2026-10-20T08:00:00+00:00",
        assignment_name="Weekly brief",
        agent_name="Analyst",
        human_input_question=question,
        result_summary=summary,
    )


def test_a_run_waiting_on_a_human_is_urgent_and_unread_until_answered() -> None:
    item = present_run(_run("awaiting_human", question="Approve isolating 3 gateways?"))
    assert item is not None
    assert (item.kind, item.source, item.route, item.run_id) == (
        "decision",
        "run",
        f"/operations/executions/{RUN_ID}",
        RUN_ID,
    )
    assert item.awaiting_answer is True
    assert is_unread(item, read_at=datetime(2030, 1, 1, tzinfo=UTC), dismissed=set()) is True
    # #991: opening or dismissing it never reads a decision still waiting.
    assert is_unread(item, read_at=datetime(2030, 1, 1, tzinfo=UTC), dismissed={item.id}) is True
    # Answered, the run is no longer awaiting_human: an ordinary entry.
    assert present_run(_run(summary="Approved, 3 gateways isolated.")).awaiting_answer is False


def test_a_finished_run_shows_its_marker_free_summary():
    item = present_run(_run(summary=f"Done. [[REPORT:{UUID_A}]] See the report."))
    assert (item.kind, item.summary) == ("run", "Done. See the report.")
    assert present_run(_run(summary=f"[[FILE:{UUID_A}]]")) is None
    assert summary_preview("x" * 300).endswith("…") and len(summary_preview("x" * 300)) == 201


def test_waiting_items_come_first_then_the_newest_once_each():
    def item(i, kind, created):
        return NotificationItem(id=i, kind=kind, created_at=created, route="/", title=i)

    merged = merge_items(
        [
            item("old", "run", "2026-10-01T00:00:00+00:00"),
            item("hitl", "hitl", "2026-09-01T00:00:00+00:00"),
            item("new", "reminder", "2026-10-02T00:00:00+00:00"),
            item("new", "reminder", "2026-10-02T00:00:00+00:00"),
        ]
    )
    assert [i.id for i in merged] == ["hitl", "new", "old"]


def test_read_state_is_the_marker_and_the_dismissed_ids():
    item = NotificationItem(id="a", kind="run", created_at="2026-10-20T08:00:00+00:00", route="/", title="t")
    assert is_unread(item, read_at=None, dismissed=set()) is True
    assert is_unread(item, read_at=datetime(2026, 10, 20, 9, tzinfo=UTC), dismissed=set()) is False
    assert is_unread(item, read_at=datetime(2026, 10, 20, 7, tzinfo=UTC), dismissed={"a"}) is False


# ── The subscription a phone hands over is hostile ───────────────────────────


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://web.push.apple.com/QGx",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
    ],
)
def test_the_browser_push_services_are_accepted(endpoint):
    assert push_endpoint_problem(endpoint) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com.evil.example/x",
        "https://evilfcm.googleapis.com/x",
        "https://evil.example/fcm.googleapis.com",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://169.254.169.254/latest/meta-data",
        "https://fcm.googleapis.com:8443/x",
        # A port past 65535, one that is not a number, a malformed IPv6 host:
        # ``urlsplit`` raises on each, read as a refusal, never a 500.
        "https://fcm.googleapis.com:99999/x",
        "https://fcm.googleapis.com:-1/x",
        "https://fcm.googleapis.com:abc/x",
        "https://[fcm.googleapis.com/x",
        "https://[::1/x",
        "https://user:pass@fcm.googleapis.com/x",
        "https://localhost/x",
        "javascript:alert(1)",
        "https://fcm.googleapis.com/" + "a" * 2100,
        "",
        None,
        42,
    ],
)
def test_anything_else_is_refused(endpoint):
    assert push_endpoint_problem(endpoint) is not None


def _client_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = b"0123456789abcdef"
    return private, public, auth


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _subscription(endpoint="https://fcm.googleapis.com/fcm/send/abc"):
    private, public, auth = _client_keys()
    return (
        private,
        auth,
        Subscription.parse({"endpoint": endpoint, "keys": {"p256dh": _b64(public), "auth": _b64(auth)}}),
    )


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"p256dh": "AAAA", "auth": "AAAA"},
        {"p256dh": "!!!", "auth": "x"},
        {"p256dh": _b64(b"\x05" + b"x" * 64), "auth": _b64(b"y" * 16)},
    ],
)
def test_a_subscription_with_unusable_keys_is_refused(keys):
    with pytest.raises(PushEndpointError):
        Subscription.parse({"endpoint": "https://fcm.googleapis.com/x", "keys": keys})


def test_the_payload_is_encrypted_to_the_phone_alone():
    import http_ece

    private, auth, subscription = _subscription()
    body = encrypt_payload(subscription, b'{"title":"Approve?"}')
    assert b"Approve" not in body
    assert http_ece.decrypt(body, private_key=private, auth_secret=auth, version="aes128gcm") == b'{"title":"Approve?"}'


def test_the_vapid_token_names_the_push_service_and_expires(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives import serialization

    from app.core.config import settings

    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    public, pem = _generate_vapid_keys()
    header = vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", VapidKeys(public, pem), now=1_800_000_000)
    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert header.endswith(f", k={public}")
    public_key = serialization.load_pem_private_key(pem.encode(), None).public_key()
    claims = jwt.decode(
        token, public_key, algorithms=["ES256"], audience="https://fcm.googleapis.com", options={"verify_exp": False}
    )
    assert claims == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_800_000_000 + 12 * 3600,
        "sub": "https://xtm.example.com",
    }


def test_a_payload_is_bounded_and_minimal_mode_says_nothing():
    item = NotificationItem(
        id="i", kind="hitl", created_at="", route="/runs/1", title="T" * 200, summary="S" * 1000, run_id="1"
    )
    full = push_payload(item, minimal=False, actions=[{"action": "approve", "title": "Approve", "token": "x" * 40}])
    assert (len(full["title"]), len(full["body"]), full["url"], full["urgent"]) == (80, 240, "/runs/1", True)
    assert [a["action"] for a in full["actions"]] == ["approve"]
    minimal = push_payload(item, minimal=True, actions=[{"action": "approve"}])
    assert {k: minimal[k] for k in ("title", "body", "url")} == {
        "title": "New notification",
        "body": "Open the app to see it.",
        "url": "/",
    }
    assert "actions" not in minimal


@pytest.mark.parametrize(
    ("quiet", "utc_hour", "expected"),
    [
        ({"start": "22:00", "end": "07:00", "timezone": "Europe/Paris"}, 21, True),  # 23:00 in Paris
        ({"start": "22:00", "end": "07:00", "timezone": "Europe/Paris"}, 6, False),  # 08:00 in Paris
        ({"start": "12:00", "end": "14:00", "timezone": "UTC"}, 13, True),
        ({"start": "12:00", "end": "14:00", "timezone": "UTC"}, 14, False),
        ({"start": "12:00", "end": "12:00", "timezone": "UTC"}, 12, False),
        ({"start": "25:00", "end": "07:00", "timezone": "UTC"}, 3, False),
        ({"start": "22:00", "end": "07:00", "timezone": "Mars/Base"}, 3, False),
        (None, 3, False),
    ],
)
def test_quiet_hours_cross_midnight_in_the_phone_time_zone(quiet, utc_hour, expected):
    assert in_quiet_hours(quiet, datetime(2026, 10, 20, utc_hour, 0, tzinfo=UTC)) is expected


def test_a_phone_gets_only_what_it_wants():
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    phone = SimpleNamespace(push_enabled=True, muted_kinds=["reminder"], quiet_hours=None)
    assert (device_wants(phone, "run", now), device_wants(phone, "reminder", now)) == (True, False)
    assert device_wants(SimpleNamespace(push_enabled=False, muted_kinds=[], quiet_hours=None), "hitl", now) is False


# ── Sending ──────────────────────────────────────────────────────────────────


def _send(monkeypatch, status_code: int):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"Location": "https://evil.example"} if status_code == 302 else {}
        # A body the client streams, as a push service's answer is read.
        return httpx.Response(status_code, headers=headers, stream=httpx.ByteStream(b"{}"))

    async def _no_dns(url, **_kw):
        return url

    import app.services.web_fetch as web_fetch

    monkeypatch.setattr(web_fetch, "validate_outbound_url_async", _no_dns)
    monkeypatch.setattr(web_fetch, "guarded_client_options", lambda **_kw: {"transport": httpx.MockTransport(handler)})
    _private, _auth, subscription = _subscription()
    keys = VapidKeys(*_generate_vapid_keys())
    result = asyncio.run(send_push(subscription, {"title": "t"}, keys, urgent=True))
    return result, seen


@pytest.mark.parametrize(
    ("status_code", "outcome"), [(201, "sent"), (404, "gone"), (410, "gone"), (500, "failed"), (302, "failed")]
)
def test_a_push_reports_what_the_service_answered(monkeypatch, status_code, outcome):
    result, seen = _send(monkeypatch, status_code)
    assert result.outcome == outcome
    [request] = seen  # never a second request: no redirect is followed
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert request.headers["Urgency"] == "high"
    assert request.headers["Authorization"].startswith("vapid t=")


def test_an_endpoint_outside_the_push_services_is_never_contacted(monkeypatch):
    called: list = []
    monkeypatch.setattr("app.services.web_fetch.guarded_client_options", lambda **_kw: called.append(1) or {})
    _private, public, auth = _client_keys()
    forged = Subscription(endpoint="https://evil.example/x", p256dh=public, auth=auth)
    result = asyncio.run(send_push(forged, {}, VapidKeys(*_generate_vapid_keys()), urgent=False))
    assert (result.outcome, called) == ("refused", [])


# ── One-time action tokens ───────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, object] = {}

    def pipeline(self):
        return self

    def set(self, key, value, ex=None):
        self.store[key] = value

    def sadd(self, key, value):
        self.store.setdefault(key, set()).add(value)

    def expire(self, key, seconds):
        return None

    async def execute(self):
        return None

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def smembers(self, key):
        return set(self.store.get(key, set()))

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


def test_a_token_is_spent_once_and_names_exactly_what_was_pending(fake_redis):
    from app.services.push_action_tokens import approval_actions, consume_token

    run_id, user_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pending = [{"tool_call_id": "b"}, {"tool_call_id": "a"}, {"tool_name": "no id"}]
    actions = asyncio.run(approval_actions(run_id=run_id, user_id=user_id, device_id=device_id, pending=pending))
    assert [a["action"] for a in actions] == ["approve", "reject"]
    # Stored by digest only: the token itself is nowhere in Redis.
    assert all(a["token"] not in json.dumps(list(fake_redis.store), default=str) for a in actions)
    first = asyncio.run(consume_token(actions[0]["token"]))
    assert (first.decision, first.tool_call_ids, first.device_id) == ("approve", ("a", "b"), device_id)
    assert asyncio.run(consume_token(actions[0]["token"])) is None


def test_deciding_a_run_or_unlinking_a_phone_voids_its_buttons(fake_redis):
    from app.services.push_action_tokens import approval_actions, consume_token, void_device_tokens, void_run_tokens

    run_id, user_id = uuid.uuid4(), uuid.uuid4()
    phone_a, phone_b = uuid.uuid4(), uuid.uuid4()
    pending = [{"tool_call_id": "a"}]
    on_a = asyncio.run(approval_actions(run_id=run_id, user_id=user_id, device_id=phone_a, pending=pending))
    on_b = asyncio.run(approval_actions(run_id=uuid.uuid4(), user_id=user_id, device_id=phone_b, pending=pending))
    asyncio.run(void_run_tokens(run_id))
    assert [asyncio.run(consume_token(a["token"])) for a in on_a] == [None, None]
    asyncio.run(void_device_tokens(phone_b))
    assert asyncio.run(consume_token(on_b[1]["token"])) is None


@pytest.mark.parametrize("token", [None, "", "short", "x" * 101, 12])
def test_a_malformed_token_is_refused_without_a_lookup(fake_redis, token):
    from app.services.push_action_tokens import consume_token

    assert asyncio.run(consume_token(token)) is None


def test_no_redis_means_no_buttons_and_no_decision(monkeypatch):
    """Fail closed: a token that cannot be stored or checked is never honoured."""
    from app.services.push_action_tokens import approval_actions, consume_token

    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.core.redis.redis_client", _down)
    assert (
        asyncio.run(
            approval_actions(
                run_id=uuid.uuid4(), user_id=uuid.uuid4(), device_id=uuid.uuid4(), pending=[{"tool_call_id": "a"}]
            )
        )
        is None
    )
    assert asyncio.run(consume_token("x" * 40)) is None


# ── The agent tool ───────────────────────────────────────────────────────────


def _phone_tool(user_id):
    from app.services.builtin_tools import build_phone_tools

    _defs, executor = build_phone_tools(None, user_id or "", agent_name="Analyst")
    return executor


def _call(executor, arguments):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="c", name="notify_my_phone", arguments=arguments)))


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"title": "", "message": "x"}, "both required"),
        ({"title": "T" * 81, "message": "x"}, "under 80"),
        ({"title": "T", "message": "M" * 301}, "under 300"),
        ({"title": "T", "message": "M", "path": "https://evil.example"}, "path must be a page of this platform's web app"),
        ({"title": "T", "message": "M", "path": "/api/v1/users"}, "path must be a page of this platform's web app"),
    ],
)
def test_the_tool_refuses_what_is_out_of_bounds(arguments, fragment):
    result = _call(_phone_tool(str(uuid.uuid4())), arguments)
    assert result.is_error is True
    assert fragment in result.content


def test_the_tool_needs_a_signed_in_user():
    result = _call(_phone_tool(None), {"title": "T", "message": "M"})
    assert (result.is_error, result.content) == (True, "Phone notifications can only reach a signed-in user.")


def test_every_agentic_entry_point_wires_the_phone_suite():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "services"
    for module in ("agent_chat", "general_chat", "assignment_engine"):
        source = (root / f"{module}.py").read_text(encoding="utf-8")
        assert '"phone" in ' in source and "build_phone_tools(" in source, module


def test_minimal_mode_reads_the_security_option(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "phone_notification_content", "minimal")
    assert mobile_push.phone_notifications_minimal() is True


def test_a_push_that_cannot_be_queued_never_costs_the_notification(caplog):
    class _NoSession:
        """A session the push cannot hook into (it raises on ``sync_session``)."""

        @property
        def sync_session(self):
            raise RuntimeError("no sync session")

    with caplog.at_level(logging.WARNING, logger="app.services.mobile_push"):
        assert mobile_push.queue_push(_NoSession(), user_id="u", ref_kind="activity", ref_id="a1") is None
    assert [r.getMessage() for r in caplog.records] == ["Could not queue the phone push of activity a1"]
