"""Jira dynamic webhooks for integration_event assignments (#11036, slice 2)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.services import jira_events, jira_webhooks, trigger_provisioning

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
URL = "https://xtm.example.com/api/hooks/integrations/i-1"
ME = "acc-me"


class FakeJira:
    """Records the webhook REST calls and answers them like Jira."""

    def __init__(self, *, register_errors=None, refresh_ok=True):
        self.calls: list[tuple[str, str, object]] = []
        self.next_id = 1000
        self.register_errors = register_errors
        self.refresh_ok = refresh_ok

    async def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "post" and path == "webhook":
            if self.register_errors:
                return {"webhookRegistrationResult": [{"errors": self.register_errors}]}, None
            self.next_id += 1
            return {"webhookRegistrationResult": [{"createdWebhookId": self.next_id}]}, None
        if method == "put" and path == "webhook/refresh":
            if not self.refresh_ok:
                return None, "Jira API error (404)"
            return {"expirationDate": int((NOW + timedelta(days=30)).timestamp() * 1000)}, None
        if method == "delete":
            return {}, None
        return None, "unexpected call"


# ── JQL, events, URL ─────────────────────────────────────────────────────────


def test_webhook_jql_uses_only_the_clauses_jira_evaluates_on_a_webhook():
    jql = jira_webhooks.webhook_jql(
        {"assignee_account_id": "self", "project_key": ["OPS"], "issue_type": "Task", "status": "To Do", "jql": "x"},
        self_account_id=ME,
    )
    assert jql == 'assignee in ("acc-me") AND project in ("OPS") AND issuetype in ("Task") AND status in ("To Do")'


def test_webhook_jql_registers_nothing_for_an_unnarrowed_filter_or_an_unknown_self():
    assert jira_webhooks.webhook_jql({}) is None
    assert jira_webhooks.webhook_jql({"jql": "labels = x"}) is None
    assert jira_webhooks.webhook_jql({"assignee_account_id": "self"}, self_account_id="") is None


def test_each_trigger_event_maps_to_the_jira_webhook_events():
    assert jira_webhooks.webhook_events("issue_assigned") == ["jira:issue_updated"]
    assert jira_webhooks.webhook_events("issue_created") == ["jira:issue_created"]
    assert jira_webhooks.webhook_events("comment_created") == ["comment_created"]
    assert jira_webhooks.webhook_events("nope") == []


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://xtm.example.com/", "https://xtm.example.com/api/hooks/integrations/i-1"),
        ("http://xtm.example.com", None),
        ("https://localhost:8000", None),
        ("https://10.0.0.4", None),
        ("https://copilot.internal", None),
    ],
)
def test_public_receiver_url(base, expected):
    assert jira_webhooks.public_receiver_url(base, "i-1") == expected


def test_the_provisioner_is_registered_for_jira():
    assert trigger_provisioning._PROVISIONERS["jira"] == "app.services.jira_webhooks"


# ── Registration, refresh, replacement ───────────────────────────────────────


async def test_reconcile_registers_a_new_webhook():
    fake = FakeJira()
    state = await jira_webhooks.reconcile(
        fake, None, url=URL, jql='assignee in ("acc-me")', events=["jira:issue_updated"], now=NOW
    )
    assert state["status"] == "registered" and state["webhook_ids"] == [1001]
    assert fake.calls == [
        (
            "post",
            "webhook",
            {"url": URL, "webhooks": [{"jqlFilter": 'assignee in ("acc-me")', "events": ["jira:issue_updated"]}]},
        )
    ]
    assert datetime.fromisoformat(state["expires_at"]) == NOW + timedelta(days=30)


async def test_reconcile_leaves_a_fresh_webhook_alone():
    fake = FakeJira()
    state = await jira_webhooks.reconcile(fake, None, url=URL, jql="j", events=["e"], now=NOW)
    fake.calls.clear()
    again = await jira_webhooks.reconcile(fake, state, url=URL, jql="j", events=["e"], now=NOW + timedelta(days=1))
    assert fake.calls == [] and again["webhook_ids"] == state["webhook_ids"]


async def test_reconcile_refreshes_before_the_30_day_expiry():
    fake = FakeJira()
    state = await jira_webhooks.reconcile(fake, None, url=URL, jql="j", events=["e"], now=NOW - timedelta(days=25))
    fake.calls.clear()
    again = await jira_webhooks.reconcile(fake, state, url=URL, jql="j", events=["e"], now=NOW)
    assert fake.calls == [("put", "webhook/refresh", {"webhookIds": state["webhook_ids"]})]
    assert datetime.fromisoformat(again["expires_at"]) == NOW + timedelta(days=30)
    assert again["webhook_ids"] == state["webhook_ids"]


async def test_reconcile_registers_again_when_the_refresh_is_refused():
    fake = FakeJira(refresh_ok=False)
    state = await jira_webhooks.reconcile(fake, None, url=URL, jql="j", events=["e"], now=NOW - timedelta(days=25))
    fake.calls.clear()
    again = await jira_webhooks.reconcile(fake, state, url=URL, jql="j", events=["e"], now=NOW)
    assert [c[:2] for c in fake.calls] == [("put", "webhook/refresh"), ("delete", "webhook"), ("post", "webhook")]
    assert again["status"] == "registered" and again["webhook_ids"] != state["webhook_ids"]


async def test_reconcile_replaces_the_webhook_when_the_filter_changed():
    fake = FakeJira()
    state = await jira_webhooks.reconcile(fake, None, url=URL, jql="old", events=["e"], now=NOW)
    fake.calls.clear()
    again = await jira_webhooks.reconcile(fake, state, url=URL, jql="new", events=["e"], now=NOW)
    assert fake.calls[0] == ("delete", "webhook", {"webhookIds": state["webhook_ids"]})
    assert again["jql"] == "new"


async def test_a_refused_registration_keeps_the_poll_with_the_reason():
    fake = FakeJira(register_errors=["The clause labels is unsupported"])
    state = await jira_webhooks.reconcile(fake, None, url=URL, jql="labels = x", events=["e"], now=NOW)
    assert state["status"] == "error" and "unsupported" in state["error"] and state["webhook_ids"] == []
    assert jira_webhooks.due(state, NOW + timedelta(seconds=jira_webhooks.RETRY_SECONDS))
    assert not jira_webhooks.due(state, NOW + timedelta(minutes=5))


# ── provision_one / deprovision through the database side ────────────────────


class _Assignment:
    def __init__(self, config, enabled=True):
        self.id = "a-1"
        self.agent_id = "g-1"
        self.trigger_config = config
        self.enabled = enabled


def _patch_connection(monkeypatch, fake):
    async def _conn(db, assignment, agent):
        return fake, "i-1", ME

    monkeypatch.setattr(jira_webhooks, "_connection", _conn)
    from app.core.config import settings

    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    monkeypatch.setattr(settings, "integration_event_jira_webhooks_enabled", True)


async def test_provision_one_registers_for_an_enabled_assignment(monkeypatch):
    fake = FakeJira()
    _patch_connection(monkeypatch, fake)
    wf = _Assignment({"provider": "jira", "event": "issue_assigned", "filter": {"assignee_account_id": "self"}})
    state = await jira_webhooks.provision_one(None, wf, object(), now=NOW)
    assert state["status"] == "registered" and state["account_id"] == ME and state["integration_id"] == "i-1"
    assert wf.trigger_config["jira_webhook"] == state
    assert fake.calls[0][2]["webhooks"][0]["jqlFilter"] == 'assignee in ("acc-me")'


async def test_provision_one_deletes_the_webhook_of_a_disabled_assignment(monkeypatch):
    fake = FakeJira()
    _patch_connection(monkeypatch, fake)
    wf = _Assignment(
        {"provider": "jira", "event": "issue_assigned", "jira_webhook": {"status": "registered", "webhook_ids": [7]}},
        enabled=False,
    )
    assert await jira_webhooks.provision_one(None, wf, object(), now=NOW) is None
    assert fake.calls == [("delete", "webhook", {"webhookIds": [7]})]
    assert "jira_webhook" not in wf.trigger_config


async def test_provision_one_polls_only_an_unnarrowed_filter(monkeypatch):
    fake = FakeJira()
    _patch_connection(monkeypatch, fake)
    wf = _Assignment({"provider": "jira", "event": "issue_updated"})
    state = await jira_webhooks.provision_one(None, wf, object(), now=NOW)
    assert state["status"] == "polled" and fake.calls == []


async def test_deprovision_deletes_the_registered_webhook(monkeypatch):
    fake = FakeJira()

    async def _conn(db, assignment, agent):
        return fake, "i-1", ME

    class _Db:
        async def get(self, model, key):
            return object()

    monkeypatch.setattr(jira_webhooks, "_connection", _conn)
    wf = _Assignment({"provider": "jira", "jira_webhook": {"webhook_ids": [5, 6]}})
    await jira_webhooks.deprovision(_Db(), wf)
    assert fake.calls == [("delete", "webhook", {"webhookIds": [5, 6]})]


# ── Delivery: verification and mapping ───────────────────────────────────────


def _token(secret: str, exp_in: int = 300) -> str:
    return jwt.encode({"iss": "jira", "exp": int(time.time()) + exp_in}, secret, algorithm="HS256")


def test_verify_delivery_accepts_only_a_token_signed_with_the_client_secret():
    assert jira_webhooks.verify_delivery(f"Bearer {_token('s3cret')}", "s3cret")
    assert not jira_webhooks.verify_delivery(f"Bearer {_token('other')}", "s3cret")
    assert not jira_webhooks.verify_delivery(f"Bearer {_token('s3cret', exp_in=-3600)}", "s3cret")
    assert not jira_webhooks.verify_delivery(None, "s3cret")
    assert not jira_webhooks.verify_delivery(f"Bearer {_token('s3cret')}", "")
    assert not jira_webhooks.verify_delivery(f"Basic {_token('s3cret')}", "s3cret")


def _delivery(**extra) -> dict:
    payload = {
        "timestamp": int(NOW.timestamp() * 1000),
        "webhookEvent": "jira:issue_updated",
        "user": {"accountId": "acc-boss", "displayName": "Boss"},
        "issue": {
            "id": "10001",
            "key": "OPS-7",
            "self": "https://api.atlassian.com/ex/jira/c/rest/api/3/issue/10001",
            "fields": {
                "summary": "Renew the cert",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
                "project": {"key": "OPS", "name": "Ops"},
                "assignee": {"accountId": ME, "displayName": "Me"},
            },
        },
        "changelog": {
            "id": "555",
            "items": [{"field": "assignee", "from": "acc-old", "fromString": "Old", "to": ME, "toString": "Me"}],
        },
    }
    payload.update(extra)
    return payload


def test_a_delivery_maps_to_the_polls_payload_and_message_id():
    events = jira_webhooks.delivery_events(_delivery(), "issue_assigned")
    assert len(events) == 1
    event = events[0]
    assert event["message_id"] == "jira:OPS-7:history:555"
    assert event["assignee"] == {"account_id": ME, "display_name": "Me"}
    assert event["previous_assignee"] == {"account_id": "acc-old", "display_name": "Old"}
    assert event["actor"]["account_id"] == "acc-boss"
    assert event["issue_key"] == "OPS-7" and event["project"]["key"] == "OPS"
    # The same change read by the poll carries the same id: the seen set runs it once.
    polled = jira_events.issue_events(
        {
            "key": "OPS-7",
            "fields": _delivery()["issue"]["fields"],
            "changelog": {
                "histories": [
                    {
                        "id": "555",
                        "created": "2026-11-11T10:00:00.000+0000",
                        "author": {"accountId": "acc-boss"},
                        "items": _delivery()["changelog"]["items"],
                    }
                ]
            },
        },
        "issue_assigned",
        since=NOW - timedelta(hours=1),
    )
    assert polled[0]["message_id"] == event["message_id"]


def test_a_delivery_without_the_events_change_starts_nothing():
    status_only = _delivery(changelog={"id": "556", "items": [{"field": "status", "toString": "Done"}]})
    assert jira_webhooks.delivery_events(status_only, "issue_assigned") == []
    assert jira_webhooks.delivery_events(_delivery(webhookEvent="jira:issue_created"), "issue_assigned") == []
    assert jira_webhooks.delivery_events({"webhookEvent": "jira:issue_updated"}, "issue_assigned") == []


def test_a_comment_delivery_maps_to_comment_created():
    payload = _delivery(
        webhookEvent="comment_created",
        changelog=None,
        comment={"id": "77", "created": "2026-11-11T10:00:00.000+0000", "author": {"accountId": "acc-x"}, "body": "hi"},
    )
    events = jira_webhooks.delivery_events(payload, "comment_created")
    assert [e["message_id"] for e in events] == ["jira:OPS-7:comment:77"]
    assert events[0]["comment"]["body"] == "hi"


def test_the_poll_slows_to_a_safety_net_once_the_webhook_delivers():
    assert jira_webhooks.poll_interval(60, delivered=False) == 60
    assert jira_webhooks.poll_interval(60, delivered=True) == jira_webhooks.SAFETY_POLL_SECONDS


# ── Delivery: the filter's free jql ──────────────────────────────────────────


class _SearchJira:
    """Answers the jql check search: *issues* matched, or an error."""

    def __init__(self, issues=None, err=None):
        self.calls: list[tuple[str, str, object]] = []
        self.issues = issues or []
        self.err = err

    async def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if self.err:
            return None, self.err
        return {"issues": self.issues}, None


async def test_jql_matches_searches_the_issue_key_anded_with_the_free_jql():
    fake = _SearchJira(issues=[{"id": "10001"}])
    assert await jira_webhooks.jql_matches(fake, "OPS-7", 'status = "Awaiting approval"') is True
    assert fake.calls == [
        (
            "post",
            "search/jql",
            {"jql": 'issuekey = "OPS-7" AND (status = "Awaiting approval")', "fields": ["id"], "maxResults": 1},
        )
    ]
    assert await jira_webhooks.jql_matches(_SearchJira(), "OPS-7", "labels = x") is False
    assert await jira_webhooks.jql_matches(_SearchJira(err="Jira API error (400)"), "OPS-7", "bad") is None
    assert await jira_webhooks.jql_matches(_SearchJira(), "", "labels = x") is None


class _Wf:
    def __init__(self, config):
        self.id = "a-1"
        self.name = "Legal approvals"
        self.agent_id = "g-1"
        self.trigger_config = config
        self.ignore_self_triggered_events = False


class _Redis:
    async def set(self, *args, **kwargs):
        return True


def _patch_dispatch(monkeypatch, wf, search):
    """Wires dispatch_delivery to one assignment, a fake search and recorders."""
    import contextlib

    from app.core import database, redis
    from app.services import assignment_engine, docusign_connect, integration_event_filters, slack_events

    seen: dict[str, list] = {"runs": [], "filtered": []}

    @contextlib.asynccontextmanager
    async def _session():
        yield None

    async def _redis():
        return _Redis()

    async def _assignments(db, integration_id, provider, events):
        return [(wf, type("A", (), {"name": "Legal"})())]

    async def _claim(redis_, assignment_id, msg_id):
        return True

    async def _record(assignment_id, count):
        seen["filtered"].append((assignment_id, count))

    async def _trigger(wf_, agent, payload, **kwargs):
        seen["runs"].append(payload["message_id"])
        return type("R", (), {"status": "started", "run_id": "r-1"})()

    async def _conn(db, assignment, agent):
        if search is None:
            return "no Jira token could be resolved for the integration"
        return search, "i-1", ME

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(redis, "redis_client", _redis)
    monkeypatch.setattr(docusign_connect, "provider_assignments", _assignments)
    monkeypatch.setattr(slack_events, "claim_seen", _claim)
    monkeypatch.setattr(integration_event_filters, "record_filtered_events", _record)
    monkeypatch.setattr(assignment_engine, "trigger_or_queue_assignment", _trigger)
    monkeypatch.setattr(jira_webhooks, "_connection", _conn)
    return seen


def _jql_assignment(jql='status = "Awaiting approval"'):
    flt = {"project_key": "OPS"}
    if jql:
        flt["jql"] = jql
    return _Wf({"provider": "jira", "event": "issue_updated", "filter": flt, "jira_webhook": {"account_id": ME}})


async def test_a_delivery_the_free_jql_excludes_starts_nothing_and_counts_filtered(monkeypatch):
    search = _SearchJira(issues=[])
    seen = _patch_dispatch(monkeypatch, _jql_assignment(), search)
    result = await jira_webhooks.dispatch_delivery("i-1", _delivery())
    assert result["count"] == 0 and seen["runs"] == []
    assert seen["filtered"] == [("a-1", 1)]
    assert search.calls[0][2]["jql"] == 'issuekey = "OPS-7" AND (status = "Awaiting approval")'


async def test_a_delivery_the_free_jql_matches_runs(monkeypatch):
    seen = _patch_dispatch(monkeypatch, _jql_assignment(), _SearchJira(issues=[{"id": "10001"}]))
    result = await jira_webhooks.dispatch_delivery("i-1", _delivery())
    assert result["count"] == 1 and seen["runs"] == ["jira:OPS-7:history:555"]
    assert seen["filtered"] == []


async def test_a_failed_jql_check_leaves_the_delivery_to_the_poll(monkeypatch):
    seen = _patch_dispatch(monkeypatch, _jql_assignment(), _SearchJira(err="Jira API error (400)"))
    assert (await jira_webhooks.dispatch_delivery("i-1", _delivery()))["count"] == 0
    assert seen["runs"] == [] and seen["filtered"] == []
    seen = _patch_dispatch(monkeypatch, _jql_assignment(), None)
    assert (await jira_webhooks.dispatch_delivery("i-1", _delivery()))["count"] == 0
    assert seen["runs"] == [] and seen["filtered"] == []


async def test_a_filter_without_jql_makes_no_search(monkeypatch):
    search = _SearchJira()
    seen = _patch_dispatch(monkeypatch, _jql_assignment(jql=""), search)
    assert (await jira_webhooks.dispatch_delivery("i-1", _delivery()))["count"] == 1
    assert search.calls == [] and seen["runs"] == ["jira:OPS-7:history:555"]
