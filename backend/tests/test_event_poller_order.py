"""Every provider poller dispatches the oldest events after its cursor first.

The providers list their latest events first. A busy assignment keeps what it could
not run at its provider and runs one event per idle tick, so the order the
poller dispatches a batch in is the order the backlog drains in: it must be the order
the events arrived in. After the cursor, a poll dispatches the OLDEST page and stops
its cursor on it, so a backlog larger than a page is read over the next polls instead
of losing everything older than the latest page. A backlog is logged once; only what
a poll really cannot fetch is a warning. Graph, Slack, Gmail and Redis are replaced
by fakes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import event_poller


class _Redis:
    def __init__(self, cursor: str | None = None, wf_id: object = None):
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
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
            self.hashes.pop(key, None)

    async def hmget(self, key, fields):
        stored = self.hashes.get(key, {})
        return [stored.get(field) for field in fields]

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def expire(self, *_a):
        return True

    async def exists(self, *_a):
        return 0

    async def zcount(self, *_a):
        return 0


def _response(data: dict):
    return SimpleNamespace(status_code=200, json=lambda: data, text="", headers={})


class _Http:
    """Answers each GET with the first route whose key is in the URL or the channel param."""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.urls: list[str] = []

    async def get(self, url, params=None, **_k):
        self.urls.append(url)
        channel = (params or {}).get("channel")
        for key, data in self.routes.items():
            if key == channel or (channel is None and key in url):
                return _response(data)
        raise AssertionError(f"unexpected GET {url} {params}")


def _wf():
    return SimpleNamespace(id=uuid.uuid4(), trigger_config={})


def _backlog_notices(caplog) -> int:
    return sum(
        1 for record in caplog.records if record.levelno == logging.INFO and "more new events than one page" in record.getMessage()
    )


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# The page and its cursor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instants", "size", "items", "cursor", "more", "cut"),
    [
        ([], 3, [], None, False, False),
        ([1, 2], 3, [1, 2], 2, False, False),
        ([1, 2, 3, 4], 3, [1, 2, 3], 3, True, False),
        # The event past the page shares its newest timestamp: back off one.
        ([1, 2, 3, 3], 3, [1, 2, 3], 2, True, False),
        ([1, 3, 3, 3, 4], 3, [1, 3, 3], 1, True, False),
        # Nothing to back off to: the events past the page at that timestamp are lost.
        ([3, 3, 3, 3], 3, [3, 3, 3], 3, True, True),
    ],
)
def test_the_oldest_page_and_where_its_cursor_stops(instants, size, items, cursor, more, cut):
    page = event_poller._oldest_page(instants, lambda instant: instant, size)
    assert (page.items, page.cursor, page.more, page.cut) == (items, cursor, more, cut)


# ---------------------------------------------------------------------------
# Microsoft mail: Graph sorts, so the page after the cursor is its oldest
# ---------------------------------------------------------------------------


def _mail(msg_id: str, minute: int) -> dict:
    return {
        "id": msg_id,
        "subject": msg_id,
        "from": {"emailAddress": {"address": "client@x.example", "name": "Client"}},
        "toRecipients": [],
        "ccRecipients": [],
        "bodyPreview": "",
        "receivedDateTime": f"2026-09-29T08:{minute:02d}:00Z",
        "sentDateTime": f"2026-09-29T08:{minute:02d}:00Z",
    }


CURSOR_ISO = "2026-09-29T07:00:00Z"
PAGE = event_poller._MICROSOFT_MAIL_PAGE


async def test_microsoft_mail_comes_out_oldest_first_and_the_cursor_on_the_newest():
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    listing = {"value": [_mail("m3", 3), _mail("m2", 2), _mail("m1", 1)]}
    events = await event_poller._poll_microsoft_emails("tok", wf, redis, _Http({"/me/messages": listing}))
    assert [e["message_id"] for e in events] == ["m1", "m2", "m3"]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-09-29T08:03:00Z"


@pytest.mark.parametrize("cursor", [True, False], ids=["after_a_cursor", "first_poll"])
async def test_after_its_cursor_a_microsoft_poll_asks_for_the_oldest_page_and_one_more(cursor):
    """A first poll (no cursor) reads the latest page by design."""
    wf = _wf()
    http = _Http({"/me/messages": {"value": []}})
    await event_poller._poll_microsoft_emails("tok", wf, _Redis(CURSOR_ISO if cursor else None, wf.id), http)
    (url,) = http.urls
    if cursor:
        assert f"$top={PAGE + 1}" in url
        assert "$orderby=receivedDateTime asc" in url
        assert f"$filter=receivedDateTime gt {CURSOR_ISO}" in url
    else:
        assert f"$top={PAGE}" in url
        assert "$orderby=receivedDateTime desc" in url
        assert "$filter" not in url


async def test_a_microsoft_backlog_is_read_a_page_at_a_time_and_nothing_is_lost(caplog):
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    mails = [_mail(f"m{minute}", minute) for minute in range(1, PAGE + 3)]
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        # Graph answers the oldest page after the cursor, and one more.
        first = await event_poller._poll_microsoft_emails(
            "tok", wf, redis, _Http({"/me/messages": {"value": mails[: PAGE + 1]}})
        )
        assert [e["message_id"] for e in first] == [f"m{minute}" for minute in range(1, PAGE + 1)]
        assert redis.values[f"event_cursor:{wf.id}"] == mails[PAGE - 1]["receivedDateTime"]
        second = await event_poller._poll_microsoft_emails(
            "tok", wf, redis, _Http({"/me/messages": {"value": mails[PAGE:]}})
        )
    assert [e["message_id"] for e in second] == [f"m{PAGE + 1}", f"m{PAGE + 2}"]
    assert redis.values[f"event_cursor:{wf.id}"] == mails[-1]["receivedDateTime"]
    assert _warnings(caplog) == [], "nothing was lost, so nothing is a warning"
    assert _backlog_notices(caplog) == 1


async def test_a_backlog_is_logged_once_until_it_drains(caplog):
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    full = _Http({"/me/messages": {"value": [_mail(f"m{minute}", minute) for minute in range(1, PAGE + 2)]}})
    partial = _Http({"/me/messages": {"value": [_mail("m1", 1)]}})
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        for http in (full, full, full):
            # A busy assignment reads the same page again until it drains.
            redis.values[f"event_cursor:{wf.id}"] = CURSOR_ISO
            await event_poller._poll_microsoft_emails("tok", wf, redis, http)
        assert _backlog_notices(caplog) == 1
        await event_poller._poll_microsoft_emails("tok", wf, redis, partial)
        await event_poller._poll_microsoft_emails("tok", wf, redis, full)
    assert _backlog_notices(caplog) == 2, "a new backlog after one drained is said again"


async def test_a_page_ending_inside_one_timestamp_backs_the_cursor_off(caplog):
    """The filter is strictly after the cursor: stopped on the page's newest
    timestamp, the mail past the page at that timestamp would never be read."""
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    mails = [_mail(f"m{minute}", minute) for minute in range(1, PAGE)] + [
        _mail("tie-a", PAGE),
        _mail("tie-b", PAGE),
    ]
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_microsoft_emails("tok", wf, redis, _Http({"/me/messages": {"value": mails}}))
    assert len(events) == PAGE
    assert redis.values[f"event_cursor:{wf.id}"] == f"2026-09-29T08:{PAGE - 1:02d}:00Z"
    assert _warnings(caplog) == []


# Graph writes receivedDateTime to the second and filters on its sub-second value:
# ``gt 08:05:00Z`` returns the mails received at 08:05:00.4 again.
SECOND_READ = "receivedDateTime ge"


async def test_a_page_sharing_one_second_with_what_follows_reads_that_second_whole(caplog):
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    mails = [_mail(f"m{i:02d}", 5) for i in range(PAGE + 5)]
    http = _Http(
        {
            "page-2": {"value": mails[12:]},
            SECOND_READ: {"value": mails[:12], "@odata.nextLink": "https://graph.example/page-2"},
            "/me/messages": {"value": mails[: PAGE + 1]},
        }
    )
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_microsoft_emails("tok", wf, redis, http)
    assert [e["message_id"] for e in events] == [m["id"] for m in mails], "nothing of that second is lost"
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-09-29T08:05:00.999Z"
    assert (
        "$filter=receivedDateTime ge 2026-09-29T08:05:00Z and receivedDateTime lt 2026-09-29T08:05:01Z"
        in http.urls[1]
    )
    assert _warnings(caplog) == []


async def test_a_cursor_left_on_a_full_second_moves_past_it_instead_of_reading_it_forever():
    """The production stall: every poll returned the same seen page and kept the cursor."""
    wf = _wf()
    stalled = "2026-09-29T08:05:00Z"
    redis = _Redis(stalled, wf.id)
    seen = [_mail(f"m{i:02d}", 5) for i in range(PAGE + 1)]
    http = _Http({SECOND_READ: {"value": seen}, "/me/messages": {"value": seen}})
    await event_poller._poll_microsoft_emails("tok", wf, redis, http)
    cursor = redis.values[f"event_cursor:{wf.id}"]
    assert cursor == "2026-09-29T08:05:00.999Z"

    later = _Http({"/me/messages": {"value": [_mail("new", 6)]}})
    events = await event_poller._poll_microsoft_emails("tok", wf, redis, later)
    assert f"$filter=receivedDateTime gt {cursor}" in later.urls[0]
    assert [e["message_id"] for e in events] == ["new"]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-09-29T08:06:00Z"


async def test_a_page_backing_the_cursor_onto_the_second_it_holds_reads_that_second():
    wf = _wf()
    redis = _Redis("2026-09-29T08:05:00Z", wf.id)
    held = [_mail(f"a{i}", 5) for i in range(3)]
    after = [_mail(f"b{i:02d}", 6) for i in range(PAGE - 2)]
    http = _Http({SECOND_READ: {"value": held}, "/me/messages": {"value": held + after}})
    events = await event_poller._poll_microsoft_emails("tok", wf, redis, http)
    assert [e["message_id"] for e in events] == ["a0", "a1", "a2"]
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-09-29T08:05:00.999Z"


async def test_a_second_past_the_read_bound_is_a_warning(caplog, monkeypatch):
    monkeypatch.setattr(event_poller, "_MICROSOFT_MAIL_SECOND_MAX_PAGES", 2)
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    mails = [_mail(f"m{i:02d}", 5) for i in range(PAGE + 1)]
    http = _Http(
        {
            "next": {"value": [], "@odata.nextLink": "https://graph.example/next"},
            SECOND_READ: {"value": mails, "@odata.nextLink": "https://graph.example/next"},
            "/me/messages": {"value": mails},
        }
    )
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_microsoft_emails("tok", wf, redis, http)
    assert len(events) == PAGE + 1
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-09-29T08:05:00.999Z"
    (warning,) = _warnings(caplog)
    assert "within one second" in warning


async def test_a_second_graph_refuses_to_list_keeps_the_page_and_says_so(caplog):
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    mails = [_mail(f"m{i:02d}", 5) for i in range(PAGE + 1)]

    class _Refusing(_Http):
        async def get(self, url, params=None, **_k):
            if SECOND_READ in url:
                self.urls.append(url)
                return SimpleNamespace(status_code=503, json=dict, text="busy", headers={})
            return await super().get(url, params=params)

    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_microsoft_emails(
            "tok", wf, redis, _Refusing({"/me/messages": {"value": mails}})
        )
    assert len(events) == PAGE
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-09-29T08:05:00Z", "read again on the next poll"
    assert any("sharing one timestamp" in warning for warning in _warnings(caplog))


# ---------------------------------------------------------------------------
# Teams: each channel is read back to the cursor
# ---------------------------------------------------------------------------


def _teams_message(msg_id: str, minute: int, second: int = 0) -> dict:
    return {
        "id": msg_id,
        "messageType": "message",
        "createdDateTime": f"2026-09-29T08:{minute:02d}:{second:02d}.000Z",
        "body": {"content": msg_id},
        "from": {"user": {"displayName": "Someone"}},
    }


class _Pages:
    """Graph pages keyed by the exact URL asked for."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages
        self.urls: list[str] = []

    async def get(self, url, **_k):
        self.urls.append(url)
        return _response(self.pages[url])


def _channel_url(channel: str) -> str:
    return f"{event_poller.GRAPH_API}/teams/T/channels/{channel}/messages"


async def test_teams_messages_come_out_oldest_first_across_channels():
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    http = _Http(
        {
            "/channels/C-A/messages": {"value": [_teams_message("a3", 3), _teams_message("a1", 1)]},
            "/channels/C-B/messages": {"value": [_teams_message("b2", 2)]},
        }
    )
    events = await event_poller._poll_microsoft_teams("tok", ["T:C-A", "T:C-B"], wf, redis, http)
    assert [e["message_id"] for e in events] == ["a1", "b2", "a3"]


async def test_teams_reads_a_channel_back_to_its_cursor_and_dispatches_the_oldest_page(caplog):
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    newest = [_teams_message(f"t{n}", 10, n) for n in range(30, 12, -1)]
    older = [_teams_message(f"t{n}", 10, n) for n in range(12, 0, -1)]
    before_cursor = _teams_message("old", 0) | {"createdDateTime": "2026-09-29T06:59:00.000Z"}
    http = _Pages(
        {
            _channel_url("C-A"): {"value": newest, "@odata.nextLink": "https://graph/page-2"},
            "https://graph/page-2": {"value": [*older, before_cursor], "@odata.nextLink": "https://graph/page-3"},
        }
    )
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_microsoft_teams("tok", ["T:C-A"], wf, redis, http)
    size = event_poller._CHANNEL_MESSAGE_PAGE
    assert [e["message_id"] for e in events] == [f"t{n}" for n in range(1, size + 1)]
    assert redis.values[f"event_cursor:{wf.id}"] == f"2026-09-29T08:10:{size:02d}.000Z"
    assert len(http.urls) == 2, "a page reaching the cursor ends the read"
    assert _warnings(caplog) == []
    assert _backlog_notices(caplog) == 1


async def test_a_teams_channel_past_the_read_bound_is_a_warning(caplog):
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    bound = event_poller._TEAMS_MAX_LIST_PAGES
    pages = {}
    for index in range(bound + 1):
        url = _channel_url("C-A") if index == 0 else f"https://graph/page-{index}"
        pages[url] = {
            "value": [_teams_message(f"p{index}", 59 - index)],
            "@odata.nextLink": f"https://graph/page-{index + 1}",
        }
    http = _Pages(pages)
    with caplog.at_level(logging.WARNING, logger=event_poller.__name__):
        await event_poller._poll_microsoft_teams("tok", ["T:C-A"], wf, redis, http)
    assert len(http.urls) == bound
    (warning,) = _warnings(caplog)
    assert "Teams channel T:C-A" in warning


async def test_a_teams_message_changed_after_the_cursor_does_not_stop_the_read():
    """Whether Graph sorts by creation or by last change, a message changed after the
    cursor may have new messages after it."""
    wf = _wf()
    redis = _Redis(CURSOR_ISO, wf.id)
    edited = _teams_message("edited", 0) | {
        "createdDateTime": "2026-09-29T06:00:00.000Z",
        "lastModifiedDateTime": "2026-09-29T08:30:00.000Z",
    }
    http = _Pages(
        {
            _channel_url("C-A"): {"value": [edited], "@odata.nextLink": "https://graph/page-2"},
            "https://graph/page-2": {"value": [_teams_message("new", 5)]},
        }
    )
    events = await event_poller._poll_microsoft_teams("tok", ["T:C-A"], wf, redis, http)
    assert [e["message_id"] for e in events] == ["new"]


# ---------------------------------------------------------------------------
# Slack: each channel is read back to the cursor
# ---------------------------------------------------------------------------


def _slack_history(*ts: str, has_more: bool = False, next_cursor: str = "") -> dict:
    data = {
        "ok": True,
        "has_more": has_more,
        "messages": [{"type": "message", "user": "U1", "text": t, "ts": t} for t in ts],
    }
    if next_cursor:
        data["response_metadata"] = {"next_cursor": next_cursor}
    return data


class _SlackPages:
    """``conversations.history`` pages keyed by (channel, cursor)."""

    def __init__(self, pages: dict[tuple[str, str], dict]):
        self.pages = pages
        self.asked: list[tuple[str, str]] = []

    async def get(self, _url, params=None, **_k):
        key = (params["channel"], params.get("cursor", ""))
        self.asked.append(key)
        return _response(self.pages[key])


async def test_slack_messages_come_out_oldest_first_across_channels():
    wf = _wf()
    redis = _Redis("1759130000.000000", wf.id)
    http = _Http(
        {
            "C-A": _slack_history("1759131000.000300", "1759131000.000100"),
            "C-B": _slack_history("1759131000.000200"),
        }
    )
    events = await event_poller._poll_slack_messages("xoxp", ["C-A", "C-B"], wf, redis, http)
    assert [e["created_at"] for e in events] == ["1759131000.000100", "1759131000.000200", "1759131000.000300"]


def _ts(n: int) -> str:
    return f"1759131000.{n:06d}"


async def test_slack_reads_a_channel_back_to_its_cursor_and_dispatches_the_oldest_page(caplog):
    wf = _wf()
    redis = _Redis("1759130000.000000", wf.id)
    http = _SlackPages(
        {
            ("C-A", ""): _slack_history(*[_ts(n) for n in range(30, 10, -1)], has_more=True, next_cursor="c2"),
            ("C-A", "c2"): _slack_history(*[_ts(n) for n in range(10, 0, -1)]),
        }
    )
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_slack_messages("xoxp", ["C-A"], wf, redis, http)
    size = event_poller._CHANNEL_MESSAGE_PAGE
    assert [e["created_at"] for e in events] == [_ts(n) for n in range(1, size + 1)]
    assert redis.values[f"event_cursor:{wf.id}"] == _ts(size)
    assert http.asked == [("C-A", ""), ("C-A", "c2")]
    assert _warnings(caplog) == []
    assert _backlog_notices(caplog) == 1


async def test_a_slack_channel_it_cannot_read_back_to_its_cursor_is_a_warning(caplog):
    wf = _wf()
    redis = _Redis("1759130000.000000", wf.id)
    http = _Http({"C-A": _slack_history(_ts(1), has_more=True)})
    with caplog.at_level(logging.WARNING, logger=event_poller.__name__):
        await event_poller._poll_slack_messages("xoxp", ["C-A"], wf, redis, http)
    (warning,) = _warnings(caplog)
    assert "Slack channel C-A" in warning


async def test_a_skipped_slack_message_moves_the_cursor_too():
    """A bot message is never dispatched: a cursor stopping short of it would read it
    again on every poll, and a channel of them would look like a backlog forever."""
    wf = _wf()
    redis = _Redis("1759130000.000000", wf.id)
    history = _slack_history(_ts(1))
    history["messages"][0]["subtype"] = "bot_message"
    assert await event_poller._poll_slack_messages("xoxp", ["C-A"], wf, redis, _Http({"C-A": history})) == []
    assert redis.values[f"event_cursor:{wf.id}"] == _ts(1)


# ---------------------------------------------------------------------------
# Gmail: the listing has no order, so a backlog is dated message by message
# ---------------------------------------------------------------------------


class _Gmail:
    """``users.messages.list`` in the order given; each message dated by *received* (epoch ms)."""

    def __init__(self, ids: list[str], next_page: bool = False, received: dict[str, int] | None = None):
        self.ids = ids
        self.next_page = next_page
        self.received = received or {}
        self.formats: list[tuple[str, str]] = []

    async def get(self, url, params=None, **_k):
        if url.endswith("/messages"):
            listing = {"messages": [{"id": i} for i in self.ids]}
            if self.next_page:
                listing["nextPageToken"] = "more"
            return _response(listing)
        msg_id = url.rsplit("/", 1)[1]
        self.formats.append((msg_id, (params or {}).get("format", "")))
        message = {"snippet": "", "payload": {"headers": [{"name": "Subject", "value": msg_id}]}}
        if msg_id in self.received:
            message["internalDate"] = str(self.received[msg_id])
        return _response(message)


GMAIL_CURSOR = 1759130000


async def test_gmail_messages_come_out_oldest_first_whatever_the_listing_order():
    """``users.messages.list`` specifies no order: sorted by internalDate,
    the id breaking a tie."""
    wf = _wf()
    received = {"g1": 1759131000000, "g2": 1759131060000, "g2b": 1759131060000, "g3": 1759131120000}
    http = _Gmail(["g2b", "g3", "g1", "g2"], received=received)
    events = await event_poller._poll_google_emails("tok", wf, _Redis(str(GMAIL_CURSOR), wf.id), http)
    assert [e["message_id"] for e in events] == ["g1", "g2", "g2b", "g3"]
    assert "internalDate" not in events[0], "the payload the agent reads is unchanged"


def _gmail_backlog(count: int) -> tuple[list[str], dict[str, int]]:
    received = {f"g{n:02d}": (GMAIL_CURSOR + 60 * n) * 1000 for n in range(1, count + 1)}
    return sorted(received, reverse=True), received


async def test_a_gmail_backlog_dispatches_its_oldest_page_and_stops_the_cursor_on_it(caplog):
    wf = _wf()
    redis = _Redis(str(GMAIL_CURSOR), wf.id)
    ids, received = _gmail_backlog(event_poller._GMAIL_PAGE + 3)
    http = _Gmail(ids, received=received)
    with caplog.at_level(logging.INFO, logger=event_poller.__name__):
        events = await event_poller._poll_google_emails("tok", wf, redis, http)
    page = [f"g{n:02d}" for n in range(1, event_poller._GMAIL_PAGE + 1)]
    assert [e["message_id"] for e in events] == page
    assert redis.values[f"event_cursor:{wf.id}"] == str(received[page[-1]] // 1000)
    assert _warnings(caplog) == []
    assert _backlog_notices(caplog) == 1

    # Busy: the same page is listed again - each message was dated once.
    redis.values[f"event_cursor:{wf.id}"] = str(GMAIL_CURSOR)
    http.formats.clear()
    await event_poller._poll_google_emails("tok", wf, redis, http)
    assert [fmt for _id, fmt in http.formats] == ["metadata"] * event_poller._GMAIL_PAGE


@pytest.mark.parametrize("next_page", [True, False], ids=["next_page", "last_page"])
async def test_a_gmail_listing_past_the_read_bound_is_a_warning(caplog, next_page):
    wf = _wf()
    ids, received = _gmail_backlog(event_poller._GMAIL_PAGE + 1)
    http = _Gmail(ids, next_page=next_page, received=received)
    with caplog.at_level(logging.WARNING, logger=event_poller.__name__):
        await event_poller._poll_google_emails("tok", wf, _Redis(str(GMAIL_CURSOR), wf.id), http)
    assert bool(_warnings(caplog)) is next_page


async def test_a_gmail_cursor_that_would_not_move_moves_one_second():
    """Should ``after:`` read the cursor's own second, a full page at that second would
    be listed again on every poll."""
    wf = _wf()
    redis = _Redis(str(GMAIL_CURSOR), wf.id)
    received = {f"g{n:02d}": GMAIL_CURSOR * 1000 + n for n in range(event_poller._GMAIL_PAGE)}
    received["later"] = (GMAIL_CURSOR + 5) * 1000
    await event_poller._poll_google_emails("tok", wf, redis, _Gmail(list(received), received=received))
    assert redis.values[f"event_cursor:{wf.id}"] == str(GMAIL_CURSOR + 1)


async def test_a_gmail_listing_that_fits_one_page_keeps_the_cursor_at_now():
    wf = _wf()
    redis = _Redis(str(GMAIL_CURSOR), wf.id)
    ids, received = _gmail_backlog(3)
    before = datetime.now(UTC) - timedelta(seconds=1)
    await event_poller._poll_google_emails("tok", wf, redis, _Gmail(ids, received=received))
    assert int(redis.values[f"event_cursor:{wf.id}"]) >= int(before.timestamp())
