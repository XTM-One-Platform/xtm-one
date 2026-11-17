"""A recording opens on the signed-in application, never on its sign-in form. A fake page, no browser.

The pre-flight passed, yet a single-page application paints its sign-in form
while it reads the session, then routes to the application: the take's
screencast starts once the page shows signed in (``signin.wait_signed_in``),
so its first frame is the application. A page that never does within the
profile's sign-in timeout stops the take like the pre-flight, with no frame.
The real-Chromium counterpart is in ``test_browser_profile_signin_chromium.py``.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.browser_profiles import signin
from app.services.browser_profiles.profiles import ProfileSnapshot
from app.services.video_studio import capture, spec

START = "https://app.example.test/"
SIGN_IN = "https://app.example.test/sign-in"
HOME = "https://app.example.test/home"
SIGN_IN_FRAME = b"frame: the sign-in form"
APP_FRAME = b"frame: the application"


async def _nothing(*_args, **_kwargs):
    return None


async def _init_script(*_args, **_kwargs):
    """``context.add_init_script``: a script that can be disposed (a page state reset's)."""
    return SimpleNamespace(dispose=_nothing)


class _Page:
    """A single-page application: its sign-in form, then - *after* seconds once opened - the application."""

    def __init__(self, after: float | None) -> None:
        self.after = after
        self.opened_at: float | None = None
        #: When its screencast was asked to start (just before the take's origin is set): on the page's
        #: monotonic clock, and on the wall clock (``time.time``, the screencast's own).
        self.screencast_at: float | None = None
        self.screencast_wall: float | None = None
        self.mouse = SimpleNamespace(move=_nothing)

    @property
    def landed(self) -> bool:
        return (
            self.opened_at is not None and self.after is not None and time.monotonic() - self.opened_at >= self.after
        )

    @property
    def url(self) -> str:
        return HOME if self.landed else SIGN_IN

    def paint(self) -> bytes:
        return APP_FRAME if self.landed else SIGN_IN_FRAME

    async def evaluate(self, _script, _args=None) -> dict:
        """What ``signin.classify`` reads of the page."""
        if self.landed:
            return {"url": HOME, "title": "Overview", "signedIn": True, "interactive": 14, "textLength": 900}
        return {"url": SIGN_IN, "title": "Sign in", "password": "input[type=password]", "interactive": 3}

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    def on(self, *_args) -> None:
        return None

    def remove_listener(self, *_args) -> None:
        return None


class _Cdp:
    """Chromium's screencast of the fake page: a frame of what it shows every 50 ms, once started."""

    def __init__(self, page: _Page) -> None:
        self.page = page
        self.handler = None
        self.painting: asyncio.Task | None = None

    def on(self, _event, handler) -> None:
        # ``Screencast.start`` subscribes, then sets the take's origin: this instant is at or before it.
        self.page.screencast_at = time.monotonic()
        self.page.screencast_wall = time.time()
        self.handler = handler

    async def send(self, method: str, _params=None) -> None:
        if method == "Page.startScreencast":
            self.painting = asyncio.get_running_loop().create_task(self._paint())
        elif method == "Page.stopScreencast" and self.painting is not None:
            self.painting.cancel()

    async def _paint(self) -> None:
        while True:
            data = base64.b64encode(self.page.paint()).decode()
            self.handler({"sessionId": 1, "metadata": {"timestamp": time.time()}, "data": data})
            await asyncio.sleep(0.05)


class _Browser:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.memo: dict = {}
        self.session_restore = None
        self.context = SimpleNamespace(add_init_script=_init_script, new_cdp_session=self._cdp)

    async def _cdp(self, _page) -> _Cdp:
        return _Cdp(self.page)

    async def goto(self, _url: str, page=None) -> None:
        self.page.opened_at = time.monotonic()


@pytest.fixture
def browser(monkeypatch):
    """``record_take`` on the fake page, the profile's pre-flight passed (``ensure_signed_in``)."""
    world = SimpleNamespace(page=None)

    def open_page(after: float | None) -> _Browser:
        world.page = _Page(after)
        rb = _Browser(world.page)
        # What start_url renders without a session: the sign-in form (the pre-flight's baseline).
        rb.memo[f"signed_out:{START}"] = signin.PageState(url=SIGN_IN, title="Sign in", password="input", interactive=3)

        @asynccontextmanager
        async def profile_browser(**kwargs):
            world.opened = kwargs
            yield rb

        monkeypatch.setattr(capture, "profile_browser", profile_browser)
        return rb

    async def saved(_rb, _snap):
        return {"cookies": []}, signin.ProbeResult(authenticated=True, url=HOME, reason="signed in")

    monkeypatch.setattr(signin, "ensure_signed_in", _nothing)
    monkeypatch.setattr(signin, "save_session", saved)
    world.open = open_page
    return world


def _profile(**login_config) -> ProfileSnapshot:
    return ProfileSnapshot(
        id="profile",
        name="Demo environment",
        start_url=START,
        login_url=None,
        login_config=login_config,
        storage_state={"cookies": [{"name": "sid", "value": "x", "domain": "app.example.test"}]},
        sign_in_timeout=5.0,
    )


def _record(profile: ProfileSnapshot, workdir: Path, **settings) -> capture.CaptureResult:
    script = spec.parse_take_script(
        spec.validate_take_script(
            {"steps": [{"action": "wait", "seconds": 0.2}], "settle_ms": 0, "end_hold_ms": 100, **settings}
        )
    )
    return asyncio.run(
        capture.record_take(
            script, workdir=str(workdir), profile=profile, start_url=START, policy=profile.policy, max_seconds=30
        )
    )


def _painted(result: capture.CaptureResult, workdir: Path) -> list[bytes]:
    return [(workdir / name).read_bytes() for _t, name in result.frames]


def test_the_first_frame_is_the_page_the_application_lands_on(browser, tmp_path):
    browser.open(after=1.0)
    result = _record(_profile(signed_in_selector="#workspace"), tmp_path)
    ended_wall = time.time()

    painted = _painted(result, tmp_path)
    assert painted and painted[0] == APP_FRAME
    assert SIGN_IN_FRAME not in painted
    # The action log counts from the moment the application showed signed in: the screencast started once the
    # page had landed, and the first step's t0 is on the screencast's clock, so it is bounded by the time that
    # clock ran (never by a wall-time threshold a loaded runner overruns). Counted from the page's opening it
    # would also hold the whole sign-in wait.
    page = browser.page
    assert page.screencast_at - page.opened_at >= page.after
    assert result.signed_in is True and 0 <= result.events[0]["t0"] <= ended_wall - page.screencast_wall


def test_a_profile_that_marks_nothing_gets_the_pre_flights_own_judgement(browser, tmp_path):
    # Read once the page rests: the sign-in form at first (a password field), the application after it.
    browser.open(after=2.0)
    result = _record(_profile(), tmp_path)

    painted = _painted(result, tmp_path)
    assert painted and painted[0] == APP_FRAME and SIGN_IN_FRAME not in painted


def test_a_browser_zoom_records_the_page_zoomed_on_the_same_frame(browser, tmp_path):
    browser.open(after=0.0)
    result = _record(_profile(), tmp_path, browser_zoom=280)
    # A 1600x900 window at 2x, zoomed to 280%: laid out 571 CSS px wide at 5.6x, painted onto the same 3200 px frame.
    assert browser.opened["viewport"] == (571, 321) and result.viewport == (571, 321)
    assert browser.opened["pixel_ratio"] == result.pixel_ratio and round(571 * result.pixel_ratio) == 3200
    (tmp_path / "plain").mkdir()
    unzoomed = _record(_profile(), tmp_path / "plain")
    assert unzoomed.viewport == (1600, 900) and unzoomed.pixel_ratio == 2.0


def test_a_page_that_never_shows_signed_in_stops_the_take_with_no_frame(browser, tmp_path):
    browser.open(after=None)
    profile = dataclasses.replace(_profile(signed_in_selector="#workspace"), sign_in_timeout=1.0)

    with pytest.raises(capture.ProfileNotAuthenticatedError) as stopped:
        _record(profile, tmp_path)

    assert str(stopped.value).startswith(f"profile not authenticated: landed on {SIGN_IN}")
    assert "signed_in_selector (#workspace) is not on the page" in str(stopped.value)
    assert not list(tmp_path.iterdir())
