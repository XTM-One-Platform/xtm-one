"""The pollers of the event types added with Graph subscriptions.

``new_teams_reply``, ``new_teams_chat_message``, ``calendar_event_changed`` and
``email_flag_changed`` start from now (their first poll runs nothing), read
after their cursor oldest first, and build the payload the notification
endpoint builds for the same event. Graph and Redis are replaced by fakes.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import event_poller

CURSOR = "2026-10-29T08:00:00Z"


class _Redis:
    def __init__(self, cursor: str | None = None, wf_id: object = None):
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict] = {}
        if cursor is not None:
            self.values[f"event_cursor:{wf_id}"] = cursor

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, nx=False, **_k):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def hmget(self, key, fields):
        return [self.hashes.get(key, {}).get(f) for f in fields]

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def expire(self, *_a):
        return True

    async def zcount(self, *_a):
        return 0


def _response(data: dict):
    return SimpleNamespace(status_code=200, json=lambda: data, text="", headers={})


class _Http:
    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.requests: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None, **_k):
        self.requests.append((url, params))
        path = url.split("?", 1)[0]
        for key, data in self.routes.items():
            if path.endswith(key):
                return _response(data)
        raise AssertionError(f"unexpected GET {url}")


def _wf(event: str, filter_config: dict | None = None):
    config = {"provider": "microsoft", "event": event}
    if filter_config:
        config["filter"] = filter_config
    return SimpleNamespace(id=uuid.uuid4(), trigger_config=config)


def _message(msg_id: str, minute: int, **extra) -> dict:
    return {
        "id": msg_id,
        "messageType": "message",
        "createdDateTime": f"2026-10-29T08:{minute:02d}:00Z",
        "lastModifiedDateTime": f"2026-10-29T08:{minute:02d}:00Z",
        "body": {"content": f"text {msg_id}"},
        "from": {"user": {"displayName": "Alice", "id": "u-1"}},
        **extra,
    }


async def test_each_new_event_type_starts_from_now():
    for poll, event in (
        (lambda wf, redis: event_poller._poll_microsoft_teams_replies("tok", ["t1:c1"], wf, redis, _Http({})), "new_teams_reply"),
        (lambda wf, redis: event_poller._poll_microsoft_chat_messages("tok", [], wf, redis, _Http({})), "new_teams_chat_message"),
        (lambda wf, redis: event_poller._poll_microsoft_calendar_changes("tok", wf, redis, _Http({})), "calendar_event_changed"),
        (lambda wf, redis: event_poller._poll_microsoft_flag_changes("tok", wf, redis, _Http({})), "email_flag_changed"),
    ):
        wf = _wf(event)
        redis = _Redis()
        assert await poll(wf, redis) == []
        assert redis.values[f"event_cursor:{wf.id}"], event


async def test_replies_after_the_cursor_come_out_oldest_first_with_their_parent():
    wf = _wf("new_teams_reply")
    redis = _Redis(CURSOR, wf.id)
    listing = {
        "value": [
            {"id": "200", "replies": [_message("202", 5), _message("201", 3)]},
            {"id": "100", "replies": [_message("101", 1), _message("050", 0) | {"createdDateTime": "2026-10-29T07:59:00Z"}]},
        ]
    }
    http = _Http({"/teams/t1/channels/c1/messages": listing})

    events = await event_poller._poll_microsoft_teams_replies("tok", ["t1:c1"], wf, redis, http)

    assert [(e["message_id"], e["parent_message_id"]) for e in events] == [("101", "100"), ("201", "200"), ("202", "200")]
    assert all(e["event"] == "new_teams_reply" and e["channel_id"] == "c1" for e in events)
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-10-29T08:05:00Z"
    assert http.requests[0][1]["$expand"] == "replies"


async def test_chat_messages_of_the_listed_chats_after_the_cursor():
    wf = _wf("new_teams_chat_message", {"chat_ids": ["19:abc@thread.v2"]})
    redis = _Redis(CURSOR, wf.id)
    listing = {"value": [_message("3", 4), _message("2", 2), _message("1", 0) | {"createdDateTime": "2026-10-29T07:00:00Z"}]}
    http = _Http({"/chats/19:abc@thread.v2/messages": listing})

    events = await event_poller._poll_microsoft_chat_messages("tok", ["19:abc@thread.v2"], wf, redis, http)

    assert [e["message_id"] for e in events] == ["2", "3"]
    assert events[0]["chat_id"] == "19:abc@thread.v2" and events[0]["event"] == "new_teams_chat_message"
    assert "lastModifiedDateTime gt 2026-10-29T08:00:00.000Z" in http.requests[0][1]["$filter"]


async def test_chats_with_no_ids_are_discovered_from_their_last_message():
    wf = _wf("new_teams_chat_message")
    redis = _Redis(CURSOR, wf.id)
    chats = {
        "value": [
            {"id": "19:new@thread.v2", "topic": "Deal room", "chatType": "group", "lastMessagePreview": {"createdDateTime": "2026-10-29T08:10:00Z"}},
            {"id": "19:old@thread.v2", "lastMessagePreview": {"createdDateTime": "2026-10-28T08:10:00Z"}},
        ]
    }
    http = _Http({"/me/chats": chats, "/chats/19:new@thread.v2/messages": {"value": [_message("9", 10)]}})

    events = await event_poller._poll_microsoft_chat_messages("tok", [], wf, redis, http)

    assert [(e["message_id"], e["chat_topic"], e["chat_type"]) for e in events] == [("9", "Deal room", "group")]


async def test_calendar_changes_carry_the_event_id_and_the_change_kind():
    wf = _wf("calendar_event_changed")
    redis = _Redis(CURSOR, wf.id)
    listing = {
        "value": [
            {"id": "moved", "createdDateTime": "2026-10-01T08:00:00Z", "lastModifiedDateTime": "2026-10-29T08:02:00.1234567Z"},
            {"id": "new", "createdDateTime": "2026-10-29T08:01:00Z", "lastModifiedDateTime": "2026-10-29T08:01:20Z"},
            {
                "id": "gone",
                "isCancelled": True,
                "createdDateTime": "2026-10-01T08:00:00Z",
                "lastModifiedDateTime": "2026-10-29T08:03:00Z",
            },
        ]
    }
    http = _Http({"/me/events": listing})

    events = await event_poller._poll_microsoft_calendar_changes("tok", wf, redis, http)

    assert [(e["event_id"], e["change_kind"]) for e in events] == [("new", "created"), ("moved", "updated"), ("gone", "cancelled")]
    assert len({e["message_id"] for e in events}) == 3
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-10-29T08:03:00Z"
    assert http.requests[0][1]["$orderby"] == "lastModifiedDateTime asc"


async def test_flag_changes_are_told_apart_from_other_mail_updates():
    wf = _wf("email_flag_changed")
    redis = _Redis(CURSOR, wf.id)

    def _mail(msg_id: str, status: str, minute: int) -> dict:
        return {
            "id": msg_id,
            "subject": msg_id,
            "from": {"emailAddress": {"address": "a@acme.example"}},
            "flag": {"flagStatus": status},
            "lastModifiedDateTime": f"2026-10-29T08:{minute:02d}:00Z",
            "receivedDateTime": "2026-10-28T08:00:00Z",
        }

    http = _Http({"/me/messages": {"value": [_mail("read-only", "notFlagged", 1), _mail("flagged", "flagged", 2)]}})
    events = await event_poller._poll_microsoft_flag_changes("tok", wf, redis, http)
    assert [(e["mail_id"], e["flag_status"], e["previous_flag_status"]) for e in events] == [("flagged", "flagged", "")]

    http = _Http({"/me/messages": {"value": [_mail("flagged", "complete", 5), _mail("read-only", "notFlagged", 6)]}})
    events = await event_poller._poll_microsoft_flag_changes("tok", wf, redis, http)
    assert [(e["mail_id"], e["flag_status"], e["previous_flag_status"]) for e in events] == [("flagged", "complete", "flagged")]


async def test_a_mail_trigger_reads_the_folder_of_its_filter_and_the_headers_it_needs():
    wf = _wf("new_email", {"folder": "Inbox/Board", "exclude_automated": True})
    http = _Http({"/messages": {"value": []}})
    await event_poller._poll_microsoft_emails("tok", wf, _Redis(CURSOR, wf.id), http)
    url = http.requests[0][0]
    assert url.startswith(f"{event_poller.GRAPH_API}/me/mailFolders/Inbox%2FBoard/messages?")
    assert "internetMessageHeaders" in url
