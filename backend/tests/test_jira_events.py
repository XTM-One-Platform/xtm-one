"""Jira issue events for integration_event assignments (#11036)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import event_poller, jira_events
from app.services.integration_event_filters import (
    FILTER_SIGNALS_KEY,
    event_filter_reason,
    filter_expression_error,
)

SINCE = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
ME = "acc-me"


class _Redis:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        self.values[key] = value
        return True

    async def delete(self, key):
        self.values.pop(key, None)


def _wf(event: str = "issue_assigned", filter_config: dict | None = None, ignore_self: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_config={"provider": "jira", "event": event, "filter": filter_config or {}},
        ignore_self_triggered_events=ignore_self,
    )


def _issue(histories=(), comments=(), created="2026-11-11T07:00:00.000+0000") -> dict:
    return {
        "id": "10001",
        "key": "OPS-7",
        "self": "https://api.atlassian.com/ex/jira/c/rest/api/3/issue/10001",
        "fields": {
            "summary": "Renew the certificate",
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Task"},
            "project": {"key": "OPS", "name": "Operations"},
            "assignee": {"accountId": ME, "displayName": "Me"},
            "reporter": {"accountId": "acc-rep", "displayName": "Reporter"},
            "created": created,
            "comment": {"comments": list(comments)},
        },
        "changelog": {"histories": list(histories)},
    }


def _assign(history_id: str, at: str, to: str = ME, frm: str = "acc-old", author: str = "acc-boss") -> dict:
    return {
        "id": history_id,
        "created": at,
        "author": {"accountId": author, "displayName": "Boss"},
        "items": [{"field": "assignee", "from": frm, "fromString": "Old", "to": to, "toString": "Me"}],
    }


def test_catalog_declares_the_five_events_as_polling_with_filter_keys():
    events = {e["event"]: e for e in PROVIDER_CATALOG["jira"]["available_events"]}
    assert set(events) == set(jira_events.EVENTS)
    assert {e["mechanism"] for e in events.values()} == {"polling"}
    for entry in events.values():
        assert set(entry["filter_keys"]) == set(jira_events.FILTER_KEYS)


def test_every_event_is_polled_every_minute():
    for event in jira_events.EVENTS:
        assert event_poller.POLL_INTERVALS[event] == 60


def test_parse_instant_reads_the_jira_offset():
    assert jira_events.parse_instant("2026-11-11T09:01:00.000+0100") == datetime(2026, 11, 11, 8, 1, tzinfo=UTC)
    assert jira_events.parse_instant("garbage").year == 1


def test_poll_jql_narrows_by_the_filter_and_resolves_self():
    jql = jira_events.poll_jql(
        "issue_assigned",
        {
            "assignee_account_id": ["self", "acc-2"],
            "project_key": "OPS",
            "issue_type": ["Task"],
            "status": "To Do",
            "jql": "labels = x",
        },
        SINCE,
        now=datetime(2026, 11, 11, 8, 10, tzinfo=UTC),
    )
    assert jql.startswith('updated >= "-12m" AND (assignee in ("acc-2") OR assignee = currentUser())')
    assert 'project in ("OPS")' in jql and 'issuetype in ("Task")' in jql and 'status in ("To Do")' in jql
    assert "(labels = x)" in jql and jql.endswith("ORDER BY updated ASC")


def test_poll_jql_reads_created_for_issue_created_and_quotes_values():
    jql = jira_events.poll_jql("issue_created", {"project_key": 'A"B'}, SINCE, now=SINCE)
    assert jql == 'created >= "-2m" AND project in ("A\\"B") ORDER BY created ASC'


def test_search_body_expands_the_changelog():
    body = jira_events.search_body("x")
    assert body["expand"] == "changelog" and "comment" in body["fields"]


def test_issue_assigned_maps_each_assignee_change_after_the_cursor():
    issue = _issue(
        histories=[
            _assign("1", "2026-11-11T07:59:00.000+0000"),
            _assign("2", "2026-11-11T08:03:00.000+0000"),
            {"id": "3", "created": "2026-11-11T08:04:00.000+0000", "items": [{"field": "summary"}]},
        ]
    )
    events = jira_events.issue_events(issue, "issue_assigned", since=SINCE)
    assert [e["message_id"] for e in events] == ["jira:OPS-7:history:2"]
    event = events[0]
    assert event["issue_key"] == "OPS-7" and event["summary"] == "Renew the certificate"
    assert event["assignee"] == {"account_id": ME, "display_name": "Me"}
    assert event["previous_assignee"] == {"account_id": "acc-old", "display_name": "Old"}
    assert event["actor"]["account_id"] == "acc-boss"
    assert event["project"] == {"key": "OPS", "name": "Operations"}
    assert event["changelog"][0]["field"] == "assignee"


def test_an_unassignment_has_no_assignee():
    issue = _issue(histories=[_assign("2", "2026-11-11T08:03:00.000+0000", to="")])
    issue["changelog"]["histories"][0]["items"][0]["toString"] = None
    (event,) = jira_events.issue_events(issue, "issue_assigned", since=SINCE)
    assert event["assignee"] is None


def test_issue_transitioned_keeps_status_changes_only():
    history = {
        "id": "9",
        "created": "2026-11-11T08:05:00.000+0000",
        "items": [{"field": "status", "fromString": "To Do", "toString": "Done"}],
    }
    (event,) = jira_events.issue_events(
        _issue(histories=[history, _assign("2", "2026-11-11T08:03:00.000+0000")]), "issue_transitioned", since=SINCE
    )
    assert event["status"] == "Done" and event["previous_status"] == "To Do"


def test_issue_updated_maps_every_change():
    history = {"id": "3", "created": "2026-11-11T08:04:00.000+0000", "items": [{"field": "summary"}]}
    events = jira_events.issue_events(
        _issue(histories=[history, _assign("2", "2026-11-11T08:03:00.000+0000")]), "issue_updated", since=SINCE
    )
    assert [e["message_id"] for e in events] == ["jira:OPS-7:history:2", "jira:OPS-7:history:3"]


def test_issue_created_and_comment_created():
    issue = _issue(
        created="2026-11-11T08:01:00.000+0000",
        comments=[
            {"id": "c1", "created": "2026-11-11T07:00:00.000+0000", "body": "old"},
            {
                "id": "c2",
                "created": "2026-11-11T08:02:00.000+0000",
                "author": {"accountId": "acc-x", "displayName": "X"},
                "body": {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Done?"}]}],
                },
            },
        ],
    )
    (created,) = jira_events.issue_events(issue, "issue_created", since=SINCE)
    assert created["message_id"] == "jira:OPS-7:created" and created["actor"]["account_id"] == "acc-rep"
    (comment,) = jira_events.issue_events(issue, "comment_created", since=SINCE)
    assert comment["message_id"] == "jira:OPS-7:comment:c2"
    assert comment["comment"] == {"id": "c2", "body": "Done?"}


def test_filter_reason_matches_self_and_the_other_keys():
    (event,) = jira_events.issue_events(
        _issue(histories=[_assign("2", "2026-11-11T08:03:00.000+0000")]), "issue_assigned", since=SINCE
    )
    assert jira_events.filter_reason(event, {"assignee_account_id": "self"}, self_account_id=ME) is None
    assert jira_events.filter_reason(event, {"assignee_account_id": "self"}, self_account_id="someone") is not None
    # Unknown connected account: ``self`` names nobody.
    assert jira_events.filter_reason(event, {"assignee_account_id": "self"}) is not None
    assert jira_events.filter_reason(event, {"project_key": ["ops"]}) is None
    assert jira_events.filter_reason(event, {"project_key": "OTHER"}) == "project is not in project_key"
    assert jira_events.filter_reason(event, {"issue_type": "Bug"}) == "issue type is not in issue_type"
    assert jira_events.filter_reason(event, {"status": "in progress"}) is None
    assert jira_events.filter_reason(event, {"jql": "labels = x"}) is None


def test_event_filter_reason_routes_jira_events_with_the_self_signal():
    (event,) = jira_events.issue_events(
        _issue(histories=[_assign("2", "2026-11-11T08:03:00.000+0000", to="acc-other")]), "issue_assigned", since=SINCE
    )
    event[FILTER_SIGNALS_KEY] = {"jira_self_account_id": ME}
    assert (
        event_filter_reason("issue_assigned", event, {"assignee_account_id": "self"})
        == "assignee is not in assignee_account_id"
    )
    event[FILTER_SIGNALS_KEY] = {"jira_self_account_id": "acc-other"}
    assert event_filter_reason("issue_assigned", event, {"assignee_account_id": "self"}) is None


def test_the_filter_keys_pass_the_save_time_check():
    assert filter_expression_error({"assignee_account_id": "self", "project_key": ["OPS"], "jql": "labels = x"}) is None


@pytest.mark.asyncio
async def test_first_poll_starts_from_now():
    redis = _Redis()
    wf = _wf()
    assert await event_poller._poll_jira_issues("cloud||tok", "issue_assigned", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"]


@pytest.mark.asyncio
async def test_poll_without_site_reads_nothing():
    wf = _wf()
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-11T08:00:00Z"})
    assert await event_poller._poll_jira_issues("||tok", "issue_assigned", wf, redis) == []


@pytest.mark.asyncio
async def test_poll_dispatches_oldest_first_moves_the_cursor_and_signals_self(monkeypatch):
    wf = _wf(filter_config={"assignee_account_id": "self"})
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-11T08:00:00Z"})
    seen: dict = {}

    async def _search(base, headers, jql):
        seen.update(base=base, jql=jql)
        return (
            [
                _issue(
                    histories=[
                        _assign("5", "2026-11-11T08:06:00.000+0000"),
                        _assign("4", "2026-11-11T08:02:00.000+0000"),
                    ]
                )
            ],
            False,
            None,
        )

    async def _me(base, headers):
        return ME

    monkeypatch.setattr(jira_events, "search_issues", _search)
    monkeypatch.setattr(jira_events, "self_account_id", _me)
    events = await event_poller._poll_jira_issues("cloud-1||tok", "issue_assigned", wf, redis, integration_id="i1")
    assert seen["base"].endswith("/ex/jira/cloud-1")
    assert "assignee = currentUser()" in seen["jql"]
    assert [e["message_id"] for e in events] == ["jira:OPS-7:history:4", "jira:OPS-7:history:5"]
    assert events[0][FILTER_SIGNALS_KEY] == {"jira_self_account_id": ME}
    assert redis.values[f"event_cursor:{wf.id}"].startswith("2026-11-11T08:06:00")
    assert redis.values["jira_self_account:i1"] == ME


@pytest.mark.asyncio
async def test_poll_skips_the_connected_accounts_own_changes(monkeypatch):
    wf = _wf(ignore_self=True)
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-11T08:00:00Z", "jira_self_account:i1": ME})

    async def _search(base, headers, jql):
        return (
            [
                _issue(
                    histories=[
                        _assign("4", "2026-11-11T08:02:00.000+0000", author=ME),
                        _assign("5", "2026-11-11T08:03:00.000+0000"),
                    ]
                )
            ],
            False,
            None,
        )

    monkeypatch.setattr(jira_events, "search_issues", _search)
    events = await event_poller._poll_jira_issues("c||tok", "issue_assigned", wf, redis, integration_id="i1")
    assert [e["message_id"] for e in events] == ["jira:OPS-7:history:5"]


@pytest.mark.asyncio
async def test_a_failed_search_keeps_the_cursor(monkeypatch):
    wf = _wf()
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-11T08:00:00Z"})

    async def _search(base, headers, jql):
        return [], False, "Jira API error 500"

    monkeypatch.setattr(jira_events, "search_issues", _search)
    assert await event_poller._poll_jira_issues("c||tok", "issue_assigned", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == b"2026-11-11T08:00:00Z"


@pytest.mark.asyncio
async def test_a_quiet_poll_moves_the_cursor_to_now(monkeypatch):
    wf = _wf()
    redis = _Redis({f"event_cursor:{wf.id}": b"2026-11-11T08:00:00Z"})

    async def _search(base, headers, jql):
        return [], False, None

    monkeypatch.setattr(jira_events, "search_issues", _search)
    assert await event_poller._poll_jira_issues("c||tok", "issue_assigned", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] != b"2026-11-11T08:00:00Z"


@pytest.mark.asyncio
async def test_poll_provider_routes_jira(monkeypatch):
    calls = []

    async def _poll(credential, event_type, wf, redis, *, integration_id=None):
        calls.append((credential, event_type, integration_id))
        return []

    monkeypatch.setattr(event_poller, "_poll_jira_issues", _poll)
    await event_poller._poll_provider(
        "jira", "comment_created", "c||t", _wf("comment_created"), _Redis(), "seen", integration_id="i9"
    )
    assert calls == [("c||t", "comment_created", "i9")]
