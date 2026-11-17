"""A relay form whose own post is still in flight is never posted again.

The sign-in posts a relay form (hidden fields back to the application) only
when the page did not post it itself. It used to read "did not post itself"
as "no navigation started less than the timeout ago": once a slow callback
outlasted the timeout, the form was posted a second time, the navigation
clock restarted and a callback slower than the timeout could end signed in
instead of failing at the redirect stage (main red, CI 41742955172).
"""

from __future__ import annotations

from typing import Any

from app.services.browser_profiles import signin


class _Frame:
    parent_frame = None
    url = "http://idp.localhost/u/stay"


class _Request:
    redirected_from = None
    frame = _Frame()
    resource_type = "document"

    def is_navigation_request(self) -> bool:
        return True


class _Page:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    def remove_listener(self, event: str, handler: Any) -> None:
        self.handlers.pop(event, None)


def test_a_post_still_waiting_for_its_answer_past_the_timeout_is_in_flight(monkeypatch):
    page = _Page()
    nav = signin._Navigation(page)
    request = _Request()
    now = [1000.0]
    monkeypatch.setattr(signin.time, "monotonic", lambda: now[0])

    page.handlers["request"](request)
    now[0] += 12.0  # the callback answers after 12 s; the timeout is 4 s

    assert nav.in_flight  # the post is still waiting: it posted itself


def test_a_post_answered_or_committed_is_no_longer_in_flight():
    page = _Page()
    nav = signin._Navigation(page)
    answered, committed = _Request(), _Request()

    page.handlers["request"](answered)
    page.handlers["requestfinished"](answered)
    assert not nav.in_flight

    page.handlers["request"](committed)
    page.handlers["framenavigated"](_Frame())
    assert not nav.in_flight


def test_the_rest_check_ignores_a_navigation_older_than_its_window(monkeypatch):
    """``_follow`` still reads the page as resting past a navigation stuck longer than its window."""
    page = _Page()
    nav = signin._Navigation(page)
    now = [1000.0]
    monkeypatch.setattr(signin.time, "monotonic", lambda: now[0])

    page.handlers["request"](_Request())
    assert nav.navigating(4.0)
    now[0] += 12.0
    assert not nav.navigating(4.0)
    assert nav.in_flight  # the relay branches still see it in flight
