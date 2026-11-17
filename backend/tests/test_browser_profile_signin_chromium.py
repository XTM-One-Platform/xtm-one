"""A browser profile signing in through a hosted identity provider, in a real Chromium.

Loopback servers only, no network, no real tenant. Chromium resolves every
``*.localhost`` name to the loopback address itself, so ``app.localhost`` and
``idp.localhost`` are two sites with cookies of their own, both admitted by a
policy double that hands every other URL to the real outbound URL policy.

The mock application sends a signed-out browser from its start page to its own
sign-in page - a link, no field - and ``/sso/start`` redirects to the mock
identity provider: a login form (email and password), a one-time-code page (a
numeric input of 6 characters and nothing in its name), a "stay signed in"
interstitial (a checkbox, a "No" button before the "Yes" submit) and a relay
form of hidden fields POSTed to the application's callback, which sets the
application's own session and sends the browser home. Each scenario switches
one thing: a TOTP secret or none, a code refused once, a relay that does not
post itself, a slow callback, a number-match prompt, a saved session holding
only the identity provider's cookies.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``, which turns
the skip into a failure.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import pytest

from app.core import totp
from app.models.browser_profile import SESSION_EXPIRED, BrowserProfile
from app.services import web_fetch
from app.services.browser_profiles import browser, profiles, signin
from app.services.video_studio import capture, spec

pytestmark = pytest.mark.chromium

STAND_INS = {"app.localhost", "idp.localhost"}
SECRET = base64.b32encode(b"neutral-test-key-0001").decode()
USERNAME = "demo.user@example.test"
PASSWORD = "correct horse battery staple"


@dataclass
class Sites:
    """The mock application and identity provider: what each scenario switches, and what they saw."""

    port: int = 0
    reject_codes: int = 0
    acs_delay: float = 0.0
    back_home: str = "redirect"  # redirect | script | refresh
    relay_posts_itself: bool = True
    push_number: str | None = None
    approved: threading.Event = field(default_factory=threading.Event)
    #: Set when the fixture stops the server: a delayed callback the browser gave up on returns at once.
    stopping: threading.Event = field(default_factory=threading.Event)
    #: How long the single-page application's session check (``/api/me``) takes to answer.
    me_delay: float = 0.0
    #: How many of the next GETs of ``/`` the server answers 503 ("unavailable", a rate limit), and when it starts:
    #: at once, or once the dashboard was served (the burst of loads that follows a sign-in).
    unavailable: int = 0
    unavailable_after_dashboard: bool = False
    #: How many GETs of the shell's script the server answers 503 (the document itself answers 200), and whether
    #: the first one is served first (the pre-flight's load).
    script_unavailable: int = 0
    script_unavailable_after_first: bool = False
    scripts_served: int = 0
    dashboard_served: bool = False
    unavailable_answered: int = 0
    codes: list[str] = field(default_factory=list)
    stay: list[dict] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    app_sessions: set[str] = field(default_factory=set)
    #: The bearer tokens the token application issued (kept by the browser in localStorage, never in a cookie).
    bearers: set[str] = field(default_factory=set)
    idp_sessions: set[str] = field(default_factory=set)

    @property
    def app(self) -> str:
        return f"http://app.localhost:{self.port}"

    @property
    def idp(self) -> str:
        return f"http://idp.localhost:{self.port}"

    def code_ok(self, code: str) -> bool:
        now = totp.current_counter(SECRET, min_remaining=0)
        return any(totp.code_for_counter(SECRET, now + step) == code for step in (-1, 0, 1, 2))


def _page(title: str, body: str, head: str = "") -> bytes:
    return f"<!doctype html><html><head><title>{title}</title>{head}</head><body>{body}</body></html>".encode()


_DASHBOARD = _page(
    "Workspace overview",
    "<header><nav>"
    + "".join(f"<a href='/section/{i}'>Section {i}</a> " for i in range(12))
    + "</nav></header><main><h1>Overview</h1>"
    + "".join(f"<button type='button'>Action {i}</button>" for i in range(6))
    + "<table>"
    + "".join(f"<tr><td>Row {i}</td><td>{i * 7}</td></tr>" for i in range(20))
    + "</table></main>",
)

#: A single-page application: its shell paints the sign-in form at once, asks ``/api/me`` whether the
#: session is signed in, and only then routes to its home. Each view has a colour of its own, read off a frame.
_SPA = _page(
    "Workspace",
    "<main id='view' style='background:#c00000;min-height:100vh'><h1>Sign in</h1><form>"
    "<input type='email' name='identifier'><input type='password' name='secret'><button>Next</button>"
    "</form></main><script>fetch('/api/me').then(r => { if (!r.ok) return;"
    " history.replaceState(null, '', '/app/home');"
    " const view = document.getElementById('view'); view.style.background = '#0040c0';"
    " view.innerHTML = '<h1>Overview</h1>' + '<button type=\"button\">Action</button>'.repeat(6); });</script>",
)


#: What a reverse proxy answers while it sheds load or rate limits: a page of its own, nothing of the application
#: (red, read off a frame).
_UNAVAILABLE = _page(
    "503 Service Temporarily Unavailable",
    "<main style='background:#c00000;min-height:100vh'><h1>503 Service Temporarily Unavailable</h1></main>",
)

#: An application keeping its session as a bearer token in localStorage, no cookie: its sign-in form's email field is
#: a text input named by ``autocomplete="email"`` alone (no name, no id), and its pages send a signed-out browser to it.
_TOKEN_LOGIN = _page(
    "Sign in",
    "<main><form id='f'><label>Email<input type='text' inputmode='email' autocomplete='email'></label>"
    "<label>Password<input type='password' autocomplete='current-password'></label><button>Sign in</button></form>"
    "</main><script>document.getElementById('f').addEventListener('submit', (e) => { e.preventDefault();"
    " const [email, password] = [...document.querySelectorAll('input')].map((i) => i.value);"
    " fetch('/tok/api/login', {method: 'POST', body: new URLSearchParams({email, password})})"
    ".then((r) => r.ok ? r.json() : null).then((b) => { if (!b) return;"
    " localStorage.setItem('bearer_token', b.token); location.replace('/tok'); }); });</script>",
)
_TOKEN_HOME = _page(
    "Workspace",
    "<main id='view'></main><script>const t = localStorage.getItem('bearer_token');"
    " if (!t) location.replace('/tok/login'); else fetch('/tok/api/me', {headers: {Authorization: 'Bearer ' + t}})"
    ".then((r) => { if (!r.ok) { location.replace('/tok/login'); return; }"
    " document.getElementById('view').innerHTML = '<h1>Overview</h1>' + '<button type=\"button\">Action</button>'.repeat(6); });"
    "</script>",
)

#: A single-page application whose document is an empty shell its script fills in.
_SHELL = _page("Workspace", "<div id='root'></div><script src='/shell.js'></script>")


class _Handler(BaseHTTPRequestHandler):
    sites: Sites

    def log_message(self, *_args) -> None:
        return

    def _send(
        self,
        status: int,
        body: bytes = b"",
        *,
        location: str = "",
        cookies: tuple[str, ...] = (),
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if location:
            self.send_header("Location", location)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json" if body.startswith(b"{") else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, name: str) -> str | None:
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        return jar[name].value if name in jar else None

    def _form(self) -> dict[str, str]:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        return {k: v[-1] for k, v in parse_qs(raw).items()}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if (self.headers.get("Host") or "").startswith("idp."):
            return self._idp_get(parsed.path, parse_qs(parsed.query))
        return self._app_get(parsed.path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if (self.headers.get("Host") or "").startswith("idp."):
            return self._idp_post(path, self._form())
        return self._app_post(path, self._form())

    # The application.

    def _app_get(self, path: str) -> None:
        s = self.sites
        if path == "/":
            if s.unavailable and (s.dashboard_served or not s.unavailable_after_dashboard):
                s.unavailable -= 1
                s.unavailable_answered += 1
                return self._send(503, _UNAVAILABLE, headers=(("Retry-After", "0"),))
            if self._cookie("app_sid") in s.app_sessions:
                s.dashboard_served = True
                return self._send(200, _DASHBOARD)
            return self._send(302, location="/login")
        if path == "/shell":
            return self._send(200, _SHELL)
        if path == "/tok":
            return self._send(200, _TOKEN_HOME)
        if path == "/tok/login":
            return self._send(200, _TOKEN_LOGIN)
        if path == "/tok/api/me":
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            return self._send(200 if bearer in s.bearers else 401, b'{"ok": true}' if bearer in s.bearers else b"{}")
        if path == "/shell.js":
            if s.script_unavailable and (s.scripts_served or not s.script_unavailable_after_first):
                s.script_unavailable -= 1
                return self._send(503, _UNAVAILABLE, headers=(("Cache-Control", "no-store"),))
            s.scripts_served += 1
            body = (
                b"document.body.style.background = '#0040c0';"
                b"document.getElementById('root').innerHTML = '<h1>Overview</h1><a href=\"/x\">Open</a>';"
            )
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if path == "/login":
            return self._send(
                200,
                _page(
                    "Sign in", "<main><h1>Sign in</h1><a href='/sso/start'>Continue with your organisation</a></main>"
                ),
            )
        if path == "/sso/start":
            return self._send(302, location=f"{s.idp}/authorize?return_to={quote(s.app + '/acs', safe='')}")
        if path in ("/app", "/app/home"):
            return self._send(200, _SPA)
        if path == "/api/me":
            signed_in = self._cookie("app_sid") in s.app_sessions
            if signed_in and s.me_delay and s.stopping.wait(s.me_delay):
                return None
            return self._send(200 if signed_in else 401, b'{"signed_in": true}' if signed_in else b"{}")
        return self._send(404, _page("Not found", "nothing"))

    def _app_post(self, path: str, form: dict[str, str]) -> None:
        s = self.sites
        if path == "/tok/api/login":
            if form.get("email") != USERNAME or form.get("password") != PASSWORD:
                return self._send(401, b"{}")
            bearer = secrets.token_hex(12)
            s.bearers.add(bearer)
            return self._send(200, json.dumps({"token": bearer}).encode())
        if path != "/acs":
            return self._send(404)
        if s.acs_delay and s.stopping.wait(s.acs_delay):
            return None
        token = form.get("SAMLResponse", "")
        if token not in s.tokens:
            return self._send(403, _page("Refused", "unknown assertion"))
        s.tokens.discard(token)
        sid = secrets.token_hex(8)
        s.app_sessions.add(sid)
        cookie = f"app_sid={sid}; Path=/; HttpOnly; SameSite=Lax"
        if s.back_home == "script":
            return self._send(200, _page("Signing in", "<script>location.replace('/')</script>"), cookies=(cookie,))
        if s.back_home == "refresh":
            return self._send(
                200,
                _page("Signing in", "<p>One moment</p>", "<meta http-equiv='refresh' content='1;url=/'>"),
                cookies=(cookie,),
            )
        return self._send(302, location="/", cookies=(cookie,))

    # The identity provider.

    def _relay(self) -> bytes:
        s = self.sites
        token = secrets.token_hex(12)
        s.tokens.add(token)
        target = unquote(self._cookie("idp_return") or quote(s.app + "/acs", safe=""))
        post = "<script>document.forms[0].submit()</script>" if s.relay_posts_itself else ""
        return _page(
            "Working",
            f"<form method='post' action='{target}'><input type='hidden' name='SAMLResponse' value='{token}'>"
            "<noscript><button type='submit'>Continue</button></noscript></form>" + post,
        )

    def _login(self, alert: str = "") -> bytes:
        note = f"<div role='alert'>{alert}</div>" if alert else ""
        return _page(
            "Sign in to your account",
            f"<main>{note}<form method='post' action='/u/login'>"
            "<input type='email' name='identifier' autocomplete='username'>"
            "<input type='password' name='secret' autocomplete='current-password'>"
            "<button>Next</button></form></main>",
        )

    def _verify(self, alert: str = "") -> bytes:
        note = f"<div role='alert'>{alert}</div>" if alert else ""
        return _page(
            "Verify your identity",
            f"<main>{note}<form method='post' action='/u/verify'>"
            "<input name='answer' inputmode='numeric' maxlength='6' autocomplete='off'>"
            "<button>Verify</button></form></main>",
        )

    def _idp_session(self) -> tuple[str, str]:
        sid = secrets.token_hex(8)
        self.sites.idp_sessions.add(sid)
        return sid, f"idp_sid={sid}; Path=/; HttpOnly; SameSite=Lax"

    def _idp_get(self, path: str, query: dict[str, list[str]]) -> None:
        s = self.sites
        if path == "/authorize":
            back = quote((query.get("return_to") or [s.app + "/acs"])[-1], safe="")
            cookie = f"idp_return={back}; Path=/; SameSite=Lax"
            if self._cookie("idp_sid") in s.idp_sessions:
                return self._send(200, self._relay(), cookies=(cookie,))
            return self._send(302, location="/u/login", cookies=(cookie,))
        if path == "/u/login":
            return self._send(200, self._login())
        if path == "/u/verify":
            return self._send(200, self._verify())
        if path == "/u/push":
            return self._send(
                200,
                _page(
                    "Approve the sign-in",
                    "<main><p>Open the authenticator app and enter this number</p>"
                    f"<div style='font-size:44px;font-weight:700'>{s.push_number}</div></main>"
                    "<script>setInterval(() => fetch('/u/push/state').then(r => r.json())"
                    ".then(st => { if (st.approved) location.href = '/u/push/done'; }), 300);</script>",
                ),
            )
        if path == "/u/push/state":
            return self._send(200, json.dumps({"approved": s.approved.is_set()}).encode())
        if path == "/u/push/done" and s.approved.is_set():
            _sid, cookie = self._idp_session()
            return self._send(302, location="/u/stay", cookies=(cookie,))
        if path == "/u/stay":
            return self._send(
                200,
                _page(
                    "Stay signed in?",
                    "<main><form method='post' action='/u/stay'>"
                    "<label><input type='checkbox' name='remember' value='1'> Do not ask again</label>"
                    "<button type='button' onclick=\"this.form.submit()\">No</button>"
                    "<button type='submit' name='choice' value='yes'>Yes</button></form></main>",
                ),
            )
        return self._send(404, _page("Not found", "nothing"))

    def _idp_post(self, path: str, form: dict[str, str]) -> None:
        s = self.sites
        if path == "/u/login":
            if form.get("identifier") != USERNAME or form.get("secret") != PASSWORD:
                return self._send(200, self._login("The account or the password is not recognised."))
            return self._send(302, location="/u/push" if s.push_number else "/u/verify")
        if path == "/u/verify":
            code = form.get("answer", "")
            s.codes.append(code)
            if not s.code_ok(code):
                return self._send(200, self._verify("That value did not work."))
            if s.reject_codes:
                s.reject_codes -= 1
                return self._send(200, self._verify("That value did not work."))
            _sid, cookie = self._idp_session()
            return self._send(302, location="/u/stay", cookies=(cookie,))
        if path == "/u/stay":
            s.stay.append(form)
            return self._send(200, self._relay())
        return self._send(404)


class _Server(ThreadingHTTPServer):
    request_queue_size = 128
    # Joined by server_close(): a handler still running at interpreter shutdown
    # writing to stderr aborts the whole pytest process (exit 134).
    daemon_threads = False


@pytest.fixture
def sites() -> Iterator[Sites]:
    state = Sites()
    server = _Server(("127.0.0.1", 0), type("Handler", (_Handler,), {"sites": state}))
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


_CHROMIUM: list[bool] = []


@pytest.fixture
async def chromium() -> None:
    """Skip (or fail under ``REQUIRE_CHROMIUM=1``) where Playwright's Chromium is missing."""
    if _CHROMIUM:
        return
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        launched = await pw.chromium.launch(headless=True)
        await launched.close()
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc) and "playwright install" not in str(exc).lower():
            raise
        if os.environ.get("REQUIRE_CHROMIUM") == "1":
            pytest.fail(f"Playwright's Chromium is required here and is not installed: {exc}")
        pytest.skip("Playwright's Chromium is not installed (python -m playwright install chromium)")
    finally:
        await pw.stop()
    _CHROMIUM.append(True)


@pytest.fixture
def updates(monkeypatch, chromium) -> list[dict]:
    """The real URL policy but for the two stand-ins; the challenge channel and the profile writes in memory."""

    async def validate(url: str, allow_private: bool = False) -> str:
        if urlparse(url).hostname in STAND_INS:
            return url
        return await web_fetch.validate_outbound_url_async(url, allow_private=allow_private)

    async def no_redis():
        return None

    async def store_screenshot(data, user_id):
        return str(uuid.uuid4())

    recorded: list[dict] = []

    async def update_profile(profile_id: str, **changes) -> None:
        recorded.append({"profile_id": profile_id, **changes})

    monkeypatch.setattr(browser, "validate_outbound_url_async", validate)
    monkeypatch.setattr(browser, "private_targets_enabled", lambda: False)
    monkeypatch.setattr(signin, "_redis", no_redis)
    monkeypatch.setattr(signin, "_update_profile", update_profile)
    monkeypatch.setattr(signin, "_store_screenshot", store_screenshot)
    signin._local_states.clear()
    signin._local_codes.clear()
    return recorded


def _profile(sites: Sites, *, totp_secret: str | None = SECRET, login_url: bool = True, **columns) -> BrowserProfile:
    profile = BrowserProfile(
        id=uuid.uuid4(),
        name="Demo environment",
        start_url=columns.pop("start_url", f"{sites.app}/"),
        login_url=f"{sites.app}/sso/start" if login_url else None,
        sign_in_timeout_seconds=columns.pop("sign_in_timeout_seconds", 30),
        session_save_timeout_seconds=15,
        mfa_timeout_seconds=columns.pop("mfa_timeout_seconds", 60),
        **columns,
    )
    creds = {"username": USERNAME, "password": PASSWORD}
    if totp_secret:
        creds["totp_secret"] = totp_secret
    profiles.write_credentials(profile, **creds)
    return profile


def _idp_only_state(sites: Sites) -> dict:
    """A saved session holding the identity provider's cookie and nothing of the application's."""
    sid = secrets.token_hex(8)
    sites.idp_sessions.add(sid)
    return {
        "cookies": [
            {
                "name": "idp_sid",
                "value": sid,
                "domain": "idp.localhost",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def _saved(updates: list[dict]) -> dict:
    saves = [u for u in updates if u.get("storage_state") is not None]
    assert saves, f"no session was saved: {updates}"
    return saves[-1]


def _cookie_hosts(state: dict) -> set[str]:
    return {str(c.get("domain") or "").lstrip(".") for c in state.get("cookies") or []}


async def _final(cid: str, timeout: float = 60.0) -> signin.SignInOutcome:
    state = await signin._wait_for_change(
        cid, not_status={signin.STATUS_RUNNING, signin.STATUS_MFA_REQUIRED}, timeout=timeout
    )
    assert state is not None
    return state


# ── A: MFA on the identity provider ───────────────────────────────────────


async def test_a_totp_secret_signs_in_through_the_identity_provider(sites, updates):
    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    assert outcome.probe and outcome.probe["authenticated"] and outcome.probe["url"] == f"{sites.app}/"
    saved = _saved(updates)
    # Saved after the callback: the application's own cookie AND the identity provider's.
    assert _cookie_hosts(saved["storage_state"]) >= {"app.localhost", "idp.localhost"}
    assert saved["probe"]["authenticated"] is True
    assert len(sites.codes) == 1 and sites.code_ok(sites.codes[0])


async def test_without_a_secret_the_code_prompt_is_a_challenge_answered_in_the_same_browser(sites, updates):
    outcome = await signin.start_sign_in(_profile(sites, totp_secret=None), user_id=None, wait_seconds=60)

    assert outcome.status == signin.STATUS_MFA_REQUIRED and outcome.kind == signin.KIND_CODE, outcome
    assert outcome.challenge_id and outcome.screenshot_file_id and outcome.expires_at
    assert outcome.stage == signin.STAGE_MFA and urlparse(outcome.last_url).path == "/u/verify"
    challenge = next(u for u in updates if u.get("mfa_challenge_id"))
    assert challenge["mfa_challenge_kind"] == signin.KIND_CODE

    answered = await signin.submit_code(outcome.challenge_id, totp.current_code(SECRET), wait_seconds=60)
    if answered.status == signin.STATUS_RUNNING:
        answered = await _final(outcome.challenge_id)

    assert answered.status == signin.STATUS_ACTIVE, answered
    assert _cookie_hosts(_saved(updates)["storage_state"]) >= {"app.localhost", "idp.localhost"}


async def test_a_refused_code_is_retried_once_on_the_next_period(sites, updates, monkeypatch):
    waited: list[int] = []

    async def next_step(secret: str, counter: int) -> None:
        waited.append(counter)

    monkeypatch.setattr(signin, "_wait_for_step", next_step)
    sites.reject_codes = 1

    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    assert len(sites.codes) == 2 and sites.codes[0] != sites.codes[1]
    assert sites.codes[1] == totp.code_for_counter(SECRET, waited[0])


async def test_a_refused_code_without_a_secret_asks_again(sites, updates):
    sites.reject_codes = 1
    outcome = await signin.start_sign_in(_profile(sites, totp_secret=None), user_id=None, wait_seconds=60)
    assert outcome.status == signin.STATUS_MFA_REQUIRED

    again = await signin.submit_code(outcome.challenge_id, totp.current_code(SECRET), wait_seconds=60)
    assert again.status == signin.STATUS_MFA_REQUIRED and again.kind == signin.KIND_CODE, again
    assert "refused" in again.message and "That value did not work." in again.message

    done = await signin.submit_code(outcome.challenge_id, totp.current_code(SECRET), wait_seconds=60)
    if done.status == signin.STATUS_RUNNING:
        done = await _final(outcome.challenge_id)
    assert done.status == signin.STATUS_ACTIVE, done


async def test_the_stay_signed_in_interstitial_is_ticked_and_accepted(sites, updates):
    sites.relay_posts_itself = False  # the relay form is posted by the sign-in
    sites.back_home = "script"

    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    # The checkbox ticked, and the form's default button ("Yes", after a "No" that is no submit) clicked.
    assert sites.stay == [{"remember": "1", "choice": "yes"}]


async def test_a_number_match_prompt_is_a_challenge_with_the_number(sites, updates):
    sites.push_number = "42"
    outcome = await signin.start_sign_in(_profile(sites, totp_secret=None), user_id=None, wait_seconds=60)

    assert outcome.status == signin.STATUS_MFA_REQUIRED and outcome.kind == signin.KIND_PUSH_NUMBER, outcome
    assert outcome.number == "42" and outcome.screenshot_file_id and "42" in outcome.message
    # A code handed to an approval is not typed anywhere.
    assert (await signin.submit_code(outcome.challenge_id, "123456", wait_seconds=1)).kind == signin.KIND_PUSH_NUMBER

    sites.approved.set()
    final = await _final(outcome.challenge_id)
    assert final.status == signin.STATUS_ACTIVE, final


# ── B and E: the application decides, and the timeout counts from the last navigation ──


async def test_a_slow_callback_is_waited_for_from_the_last_navigation(sites, updates):
    sites.acs_delay = 6.0
    sites.back_home = "refresh"
    started = time.monotonic()

    outcome = await signin.start_sign_in(_profile(sites, sign_in_timeout_seconds=8), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    # The whole flow outlasted the timeout: it is counted from the last navigation, not from the start.
    assert time.monotonic() - started > 8
    assert "app.localhost" in _cookie_hosts(_saved(updates)["storage_state"])


async def test_a_callback_slower_than_the_timeout_names_the_redirect_stage(sites, updates):
    sites.acs_delay = 12.0
    outcome = await signin.start_sign_in(_profile(sites, sign_in_timeout_seconds=4), user_id=None, wait_seconds=60)
    if outcome.status == signin.STATUS_RUNNING:
        outcome = await _final(outcome.challenge_id)

    assert outcome.status == signin.STATUS_FAILED, outcome
    assert outcome.stage == signin.STAGE_REDIRECT_CHAIN
    assert urlparse(outcome.last_url).hostname == "idp.localhost"
    assert "redirect_chain" in outcome.message and outcome.last_url in outcome.message
    assert not [u for u in updates if u.get("storage_state") is not None]  # nothing saved mid-chain


# ── C: an honest status ───────────────────────────────────────────────────


async def test_a_session_holding_only_identity_provider_cookies_reads_expired(sites, updates):
    profile = _profile(sites, totp_secret=None)
    profiles.save_storage_state(profile, _idp_only_state(sites))

    probe = await signin.probe_profile(profiles.snapshot(profile))

    assert probe.authenticated is False
    assert probe.url == f"{sites.app}/login"
    assert "without a session" in probe.reason
    profiles.record_probe(profile, probe.to_dict())
    payload = profiles.serialize_profile(profile, can_manage=True)
    assert payload["session_status"] == SESSION_EXPIRED
    assert payload["last_probe_result"]["url"] == f"{sites.app}/login"


# ── D: recordings fail fast ───────────────────────────────────────────────


def _script() -> spec.TakeScript:
    return spec.parse_take_script(spec.validate_take_script({"steps": [{"action": "wait", "seconds": 0.3}]}))


async def test_a_recording_stops_before_step_1_when_the_profile_is_not_authenticated(sites, updates):
    profile = BrowserProfile(id=uuid.uuid4(), name="Demo environment", start_url=f"{sites.app}/")
    profiles.save_storage_state(profile, _idp_only_state(sites))
    snap = profiles.snapshot(profile)
    assert not snap.renewable  # no credentials, no sign-in URL to replay the identity provider's session on

    with tempfile.TemporaryDirectory() as workdir:
        with pytest.raises(capture.ProfileNotAuthenticatedError) as caught:
            await capture.record_take(
                _script(), workdir=workdir, profile=snap, start_url=snap.start_url, policy=snap.policy, max_seconds=30
            )
        assert not list(Path(workdir).glob("*.jpg"))  # no frame, no video

    assert str(caught.value).startswith(f"profile not authenticated: landed on {sites.app}/login")


async def test_a_recording_renews_a_saved_identity_provider_session_before_step_1(sites, updates):
    profile = _profile(sites, totp_secret=None)
    profiles.write_credentials(profile, username="", password="")
    profiles.save_storage_state(profile, _idp_only_state(sites))
    snap = profiles.snapshot(profile)
    assert snap.renewable and not snap.can_sign_in

    with tempfile.TemporaryDirectory() as workdir:
        result = await capture.record_take(
            _script(), workdir=workdir, profile=snap, start_url=snap.start_url, policy=snap.policy, max_seconds=30
        )

    assert result.signed_in is True and result.frames
    assert any("signed in again" in note for note in result.notes)
    assert result.probe is not None and result.probe.authenticated
    assert "app.localhost" in _cookie_hosts(result.storage_state)


def _app_state(sites: Sites) -> dict:
    """A saved session holding the application's own cookie."""
    sid = secrets.token_hex(8)
    sites.app_sessions.add(sid)
    cookie = {"name": "app_sid", "value": sid, "domain": "app.localhost", "path": "/", "expires": -1}
    return {"cookies": [{**cookie, "httpOnly": True, "secure": False, "sameSite": "Lax"}], "origins": []}


def _mean_colour(path: Path) -> tuple[int, int, int]:
    from PIL import Image

    with Image.open(path) as frame:
        return frame.convert("RGB").resize((1, 1)).getpixel((0, 0))


async def test_a_recording_opens_on_the_application_a_single_page_app_routes_to(sites, updates):
    """The shell paints its sign-in form while it reads the session; the take starts once it is signed in."""
    sites.me_delay = 3.0
    profile = BrowserProfile(
        id=uuid.uuid4(),
        name="Demo environment",
        start_url=f"{sites.app}/app",
        sign_in_timeout_seconds=20,
        session_save_timeout_seconds=15,
    )
    profiles.save_storage_state(profile, _app_state(sites))
    snap = profiles.snapshot(profile)

    with tempfile.TemporaryDirectory() as workdir:
        result = await capture.record_take(
            _script(), workdir=workdir, profile=snap, start_url=snap.start_url, policy=snap.policy, max_seconds=60
        )
        red, _green, blue = _mean_colour(Path(workdir) / result.frames[0][1])

    assert result.signed_in is True
    assert blue > red, "the first frame of the take is the sign-in form"


async def test_the_session_storage_of_the_application_is_saved_and_put_back(sites, updates):
    snap = profiles.snapshot(_profile(sites))
    async with browser.profile_browser(policy=snap.policy, viewport=(800, 600), pixel_ratio=1) as rb:
        await rb.goto(f"{sites.app}/login")
        await rb.page.evaluate("() => sessionStorage.setItem('workspace', 'w-1')")
        state = await rb.storage_state(session_storage_of=browser.origin_of(snap.start_url))
        assert state[browser.SESSION_STORAGE_KEY] == {sites.app: {"workspace": "w-1"}}
        async with rb.fresh_page(state) as page:
            await rb.goto(f"{sites.app}/login", page=page)
            assert await page.evaluate("() => sessionStorage.getItem('workspace')") == "w-1"


def _local_keys(state: dict, origin: str) -> set[str]:
    return set(browser.local_storage_keys(state, origin))


async def test_an_application_keeping_a_bearer_token_in_local_storage_signs_in_and_reopens_signed_in(sites, updates):
    """No cookie, an email field named by autocomplete alone: the saved session carries the token and reopens."""
    profile = _profile(sites, totp_secret=None, login_url=False, start_url=f"{sites.app}/tok")
    outcome = await signin.start_sign_in(profile, user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    saved = _saved(updates)["storage_state"]
    assert not _cookie_hosts(saved)
    assert "bearer_token" in _local_keys(saved, sites.app)
    assert len(sites.bearers) == 1

    profiles.save_storage_state(profile, saved)
    probe = await signin.probe_profile(profiles.snapshot(profile))
    assert probe.authenticated, probe


async def test_a_saved_session_without_the_token_says_it_was_not_persisted(sites, updates):
    snap = profiles.snapshot(_profile(sites, totp_secret=None, login_url=False, start_url=f"{sites.app}/tok"))
    async with browser.profile_browser(policy=snap.policy, viewport=(800, 600), pixel_ratio=1) as rb:
        probe = await signin.probe_state(rb, snap, {"cookies": [], "origins": []})
    assert not probe.authenticated
    note = browser.persistence_note({"cookies": [], "origins": []}, snap.start_url, page_keys=["bearer_token"])
    assert note == (
        "session not persisted (no cookie of app.localhost; no localStorage key; held on the page but not saved: "
        "bearer_token)"
    )


# ── F: a server that cannot answer is not a signed-out application ────────


@pytest.fixture
def quick_retries(monkeypatch) -> None:
    monkeypatch.setattr(signin, "_UNAVAILABLE_BACKOFF_SECONDS", (0.2, 0.2, 0.2))


async def test_the_saved_session_is_probed_again_when_the_burst_after_a_sign_in_is_rate_limited(
    sites, updates, quick_retries
):
    """The loads after a sign-in are rate limited: the saved session's probe is opened again, never judged on a 503."""
    sites.unavailable = 2
    sites.unavailable_after_dashboard = True

    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    assert outcome.probe and outcome.probe["authenticated"] is True and outcome.probe["url"] == f"{sites.app}/"
    assert sites.unavailable_answered == 2
    assert _saved(updates)["probe"]["authenticated"] is True


async def test_a_start_url_unavailable_for_a_moment_is_opened_again(sites, updates, quick_retries):
    profile = BrowserProfile(id=uuid.uuid4(), name="Demo environment", start_url=f"{sites.app}/")
    profiles.save_storage_state(profile, _app_state(sites))
    sites.unavailable = 2

    probe = await signin.probe_profile(profiles.snapshot(profile))

    assert probe.authenticated is True, probe
    assert probe.reason == "start_url renders the application"
    assert sites.unavailable_answered == 2


async def test_a_start_url_that_stays_unavailable_fails_with_its_status_and_not_as_signed_out(
    sites, updates, quick_retries
):
    profile = BrowserProfile(id=uuid.uuid4(), name="Demo environment", start_url=f"{sites.app}/")
    profiles.save_storage_state(profile, _app_state(sites))
    sites.unavailable = 100

    probe = await signin.probe_profile(profiles.snapshot(profile))

    assert probe.authenticated is False
    assert probe.url == f"{sites.app}/"
    assert probe.reason == signin._unavailable_reason(503, "start_url")
    assert sites.unavailable_answered == len(signin._UNAVAILABLE_BACKOFF_SECONDS) + 1


async def test_an_empty_page_whose_script_was_refused_fails_with_the_script_status(sites, updates, quick_retries):
    profile = BrowserProfile(id=uuid.uuid4(), name="Demo environment", start_url=f"{sites.app}/shell")
    profiles.save_storage_state(profile, _app_state(sites))
    sites.script_unavailable = 100

    probe = await signin.probe_profile(profiles.snapshot(profile))

    assert probe.authenticated is False
    assert probe.url == f"{sites.app}/shell"
    assert probe.reason == signin._unavailable_reason(503, "start_url")


async def test_a_recording_reloads_a_take_page_the_server_refused_before_it_starts(sites, updates, quick_retries):
    """The URL matches signed_in_url_pattern and the page has no field: still never the signed-in application."""
    profile = BrowserProfile(
        id=uuid.uuid4(),
        name="Demo environment",
        start_url=f"{sites.app}/",
        login_config={"signed_in_url_pattern": r"app\.localhost:\d+/$"},
        sign_in_timeout_seconds=20,
        session_save_timeout_seconds=15,
    )
    profiles.save_storage_state(profile, _app_state(sites))
    snap = profiles.snapshot(profile)
    sites.unavailable = 1
    sites.unavailable_after_dashboard = True  # the pre-flight is served, the take's own load is refused

    with tempfile.TemporaryDirectory() as workdir:
        result = await capture.record_take(
            _script(), workdir=workdir, profile=snap, start_url=snap.start_url, policy=snap.policy, max_seconds=60
        )
        red, green, _blue = _mean_colour(Path(workdir) / result.frames[0][1])

    assert result.signed_in is True
    assert sites.unavailable_answered == 1
    assert green > 200 and red > 200, "the first frame of the take is the proxy's error page"


async def test_a_take_page_whose_script_was_refused_is_reloaded_although_its_url_matches_the_profile(
    sites, updates, quick_retries
):
    """Marks mode watches no load: the page's own Resource Timing tells its script answered 503."""
    profile = BrowserProfile(
        id=uuid.uuid4(),
        name="Demo environment",
        start_url=f"{sites.app}/shell",
        login_config={"signed_in_url_pattern": r"/shell$"},
        sign_in_timeout_seconds=20,
        session_save_timeout_seconds=15,
    )
    profiles.save_storage_state(profile, _app_state(sites))
    snap = profiles.snapshot(profile)
    sites.script_unavailable = 1
    sites.script_unavailable_after_first = True  # the pre-flight's load is served, the take's is refused

    with tempfile.TemporaryDirectory() as workdir:
        result = await capture.record_take(
            _script(), workdir=workdir, profile=snap, start_url=snap.start_url, policy=snap.policy, max_seconds=60
        )
        red, _green, blue = _mean_colour(Path(workdir) / result.frames[0][1])

    assert result.signed_in is True
    assert sites.script_unavailable == 0
    assert blue > red, "the first frame of the take is the page whose script was refused"


async def test_one_probe_retries_within_its_budget(sites, updates, quick_retries, monkeypatch):
    monkeypatch.setattr(signin, "_UNAVAILABLE_BUDGET_SECONDS", 0.0)
    profile = BrowserProfile(id=uuid.uuid4(), name="Demo environment", start_url=f"{sites.app}/")
    profiles.save_storage_state(profile, _app_state(sites))
    sites.unavailable = 100

    probe = await signin.probe_profile(profiles.snapshot(profile))

    assert probe.http_status == 503
    assert sites.unavailable_answered == 1  # no wait fits in the budget: one attempt


async def test_a_signed_out_render_the_server_refused_is_not_compared_and_not_kept(sites, updates, quick_retries):
    profile = BrowserProfile(id=uuid.uuid4(), name="Demo environment", start_url=f"{sites.app}/")
    profiles.save_storage_state(profile, _app_state(sites))
    snap = profiles.snapshot(profile)

    async with browser.profile_browser(policy=snap.policy, viewport=(800, 600), pixel_ratio=1) as rb:
        sites.unavailable = 100
        refused = await signin.signed_out_render(rb, snap)
        assert refused is not None and refused.http_error == 503
        assert f"signed_out:{snap.start_url}" not in rb.memo

        sites.unavailable = 0
        served = await signin.signed_out_render(rb, snap)
        assert served is not None and served.http_error is None and served.url == f"{sites.app}/login"
        assert rb.memo[f"signed_out:{snap.start_url}"] is served


@pytest.fixture(autouse=True)
async def _no_leftover_sign_in():
    yield
    tasks = list(signin._running.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    signin._running.clear()
