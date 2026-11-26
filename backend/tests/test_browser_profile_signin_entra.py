"""A browser profile signing in to a Microsoft-Entra-shaped flow, in a real Chromium.

Loopback servers only, no network, no real tenant. The mock reproduces the pages
an Entra account whose default MFA method is a passkey / FIDO key goes through:
a password form (``login.microsoftonline.com`` shape, with its ``#passwordError``
banner), a ``/bridge/fido`` page that starts a WebAuthn ceremony headless
Chromium cannot run, a credential picker ("Choose a way to sign in") offering the
methods by ``data-value``, a one-time-code page (``input[name=otc]``), the
"Stay signed in?" interstitial (``#idChkBx_SAOTCC_TD`` + ``#idSIButton9``), then a
relay form back to the application.

Each scenario switches one thing: a passkey bridge backed out of through its
``cancelUrl`` or through "Sign in another way", a picker with or without a code
method, a refused code retried on the next period, a wrong password, a dry run.

Skipped where Playwright's Chromium is not installed; the ``Browser tool
(Chromium)`` CI job installs it and sets ``REQUIRE_CHROMIUM=1``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import pytest

from app.core import totp
from app.models.browser_profile import SESSION_AWAITING_MFA, BrowserProfile
from app.services import web_fetch
from app.services.browser_profiles import browser, profiles, signin

pytestmark = pytest.mark.chromium

STAND_INS = {"app.localhost", "idp.localhost"}
SECRET = base64.b32encode(b"neutral-test-key-0001").decode()
USERNAME = "demo.user@example.test"
PASSWORD = "correct horse battery staple"


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


@dataclass
class Sites:
    """The mock application and Entra identity provider, and what each scenario switches."""

    port: int = 0
    reject_codes: int = 0
    #: The passkey bridge carries a ``cancelUrl`` (else the sign-in uses "Sign in another way").
    bridge_cancel_url: bool = True
    #: The bridge path contains ``/bridge/fido`` (else a passkey page detected by the WebAuthn attempt).
    bridge_by_path: bool = True
    #: The credential picker offers an authenticator-code method (else only a push).
    code_method: bool = True
    #: A two-step sign-in: the username, then a "Choose a way to sign in" method picker (a passkey row and "Use my
    #: password"), the password, then the one-time-code page.
    method_picker: bool = False
    #: The method picker offers "Use my password" (else only the passkey row).
    picker_password: bool = True
    codes: list[str] = field(default_factory=list)
    stay: list[dict] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    app_sessions: set[str] = field(default_factory=set)
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


class _Handler(BaseHTTPRequestHandler):
    sites: Sites

    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, body: bytes = b"", *, location: str = "", cookies: tuple[str, ...] = ()) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "text/html")
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
            if self._cookie("app_sid") in s.app_sessions:
                return self._send(200, _DASHBOARD)
            return self._send(302, location="/login")
        if path == "/login":
            return self._send(
                200,
                _page("Sign in", "<main><h1>Sign in</h1><a href='/sso/start'>Continue with your organisation</a></main>"),
            )
        if path == "/sso/start":
            return self._send(302, location=f"{s.idp}/authorize?return_to={quote(s.app + '/acs', safe='')}")
        return self._send(404, _page("Not found", "nothing"))

    def _app_post(self, path: str, form: dict[str, str]) -> None:
        s = self.sites
        if path != "/acs":
            return self._send(404)
        token = form.get("SAMLResponse", "")
        if token not in s.tokens:
            return self._send(403, _page("Refused", "unknown assertion"))
        s.tokens.discard(token)
        sid = secrets.token_hex(8)
        s.app_sessions.add(sid)
        return self._send(302, location="/", cookies=(f"app_sid={sid}; Path=/; HttpOnly; SameSite=Lax",))

    # The identity provider (login.microsoftonline.com / login.microsoft.com shape).

    def _relay(self) -> bytes:
        s = self.sites
        token = secrets.token_hex(12)
        s.tokens.add(token)
        return _page(
            "Working",
            f"<form method='post' action='{s.app}/acs'><input type='hidden' name='SAMLResponse' value='{token}'>"
            "<noscript><button type='submit'>Continue</button></noscript></form>"
            "<script>document.forms[0].submit()</script>",
        )

    def _login(self, error: bool = False) -> bytes:
        banner = "<div id='passwordError' role='alert'>Your account or password is incorrect.</div>" if error else ""
        return _page(
            "Sign in to your account",
            f"<main>{banner}<form method='post' action='/login'>"
            "<input type='email' name='loginfmt' autocomplete='username'>"
            "<input type='password' name='passwd' autocomplete='current-password'>"
            "<button type='submit'>Sign in</button></form></main>",
        )

    def _username(self) -> bytes:
        return _page(
            "Sign in to your account",
            "<main><form method='post' action='/login'>"
            "<input type='email' name='loginfmt' autocomplete='username'>"
            "<button type='submit'>Next</button></form></main>",
        )

    def _methods(self) -> bytes:
        rows = [
            "<div role='button' data-value='FidoKey' onclick=\"location.href='/bridge/fido'\" "
            "style='padding:12px'>Face, fingerprint, PIN or security key</div>"
        ]
        if self.sites.picker_password:
            rows.append(
                "<div role='button' tabindex='0' onclick=\"location.href='/password'\" "
                "style='padding:12px'>Use my password</div>"
            )
        return _page("Sign in", "<main><h1>Choose a way to sign in</h1>" + "".join(rows) + "</main>")

    def _password(self) -> bytes:
        return _page(
            "Enter password",
            "<main><form method='post' action='/login'>"
            f"<input type='hidden' name='loginfmt' value='{USERNAME}'>"
            "<input type='password' name='passwd' autocomplete='current-password'>"
            "<button type='submit'>Sign in</button></form></main>",
        )

    def _bridge(self) -> bytes:
        # A WebAuthn ceremony headless Chromium cannot run, plus a "Sign in another way" link.
        return _page(
            "Sign in",
            "<main><h1>Face, fingerprint, PIN or security key</h1>"
            "<p>Your device will open a security window. Follow the instructions there to sign in.</p>"
            "<a id='signInAnotherWay' href='/picker'>Sign in another way</a></main>",
            "<script>if (navigator.credentials && navigator.credentials.get) "
            "{ navigator.credentials.get({publicKey:{challenge:new Uint8Array(32)}}).catch(() => {}); }</script>",
        )

    def _picker(self) -> bytes:
        s = self.sites
        rows = []
        if s.code_method:
            rows.append(
                "<div role='button' data-value='PhoneAppOTP' onclick=\"location.href='/otc'\" "
                "style='padding:12px'>Use a verification code</div>"
            )
            rows.append(
                "<div role='button' data-value='OneWaySMS' onclick=\"location.href='/otc'\" "
                "style='padding:12px'>Text a code to my phone</div>"
            )
        rows.append(
            "<div role='button' data-value='PhoneAppNotification' onclick=\"location.href='/push'\" "
            "style='padding:12px'>Approve a request on my Microsoft Authenticator app</div>"
        )
        return _page("Verify your identity", "<main><h1>Choose a way to sign in</h1>" + "".join(rows) + "</main>")

    def _otc(self, error: bool = False) -> bytes:
        banner = "<div role='alert'>That code is incorrect. Try again.</div>" if error else ""
        return _page(
            "Enter code",
            f"<main>{banner}<form method='post' action='/otc'>"
            "<input name='otc' inputmode='numeric' maxlength='6' autocomplete='one-time-code'>"
            "<button type='submit'>Verify</button></form></main>",
        )

    def _kmsi(self) -> bytes:
        return _page(
            "Stay signed in?",
            "<main><h1>Stay signed in?</h1><form method='post' action='/kmsi'>"
            "<label><input type='checkbox' id='idChkBx_SAOTCC_TD' name='dontask' value='1'> Don't ask again "
            "for 30 days</label>"
            "<input type='submit' id='idBtn_Back' name='choice' value='No'>"
            "<input type='submit' id='idSIButton9' name='choice' value='Yes'></form></main>",
        )

    def _idp_session_cookie(self) -> str:
        sid = secrets.token_hex(8)
        self.sites.idp_sessions.add(sid)
        return f"idp_sid={sid}; Path=/; HttpOnly; SameSite=Lax"

    def _idp_get(self, path: str, query: dict[str, list[str]]) -> None:
        s = self.sites
        if path == "/authorize":
            if self._cookie("idp_sid") in s.idp_sessions:
                return self._send(200, self._relay())
            return self._send(302, location="/login")
        if path == "/login":
            return self._send(200, self._username() if s.method_picker else self._login())
        if path == "/methods":
            return self._send(200, self._methods())
        if path == "/password":
            return self._send(200, self._password())
        if path in ("/bridge/fido", "/passkey"):
            return self._send(200, self._bridge())
        if path == "/picker":
            return self._send(200, self._picker())
        if path == "/otc":
            return self._send(200, self._otc())
        if path == "/push":
            return self._send(200, _page("Approve", "<main><p>Open your authenticator app and approve.</p></main>"))
        if path == "/kmsi":
            return self._send(200, self._kmsi())
        return self._send(404, _page("Not found", "nothing"))

    def _bridge_location(self) -> str:
        s = self.sites
        base = "/bridge/fido" if s.bridge_by_path else "/passkey"
        if s.bridge_cancel_url:
            return f"{base}?cancelUrl={quote(s.idp + '/picker', safe='')}"
        return base

    def _idp_post(self, path: str, form: dict[str, str]) -> None:
        s = self.sites
        if path == "/login":
            if s.method_picker and "passwd" not in form:
                if form.get("loginfmt") != USERNAME:
                    return self._send(200, self._username())
                return self._send(302, location="/methods")
            if form.get("loginfmt") != USERNAME or form.get("passwd") != PASSWORD:
                return self._send(200, self._login(error=True))
            return self._send(302, location="/otc" if s.method_picker else self._bridge_location())
        if path == "/otc":
            code = form.get("otc", "")
            s.codes.append(code)
            if not s.code_ok(code) or s.reject_codes:
                s.reject_codes = max(0, s.reject_codes - 1)
                return self._send(200, self._otc(error=True))
            return self._send(302, location="/kmsi", cookies=(self._idp_session_cookie(),))
        if path == "/kmsi":
            s.stay.append(form)
            return self._send(200, self._relay())
        return self._send(404)


class _Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


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
        server.shutdown()
        server.server_close()
        thread.join(5)


_CHROMIUM: list[bool] = []


@pytest.fixture
async def chromium() -> None:
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


def _profile(sites: Sites, *, totp_secret: str | None = SECRET, password: str = PASSWORD, **columns) -> BrowserProfile:
    profile = BrowserProfile(
        id=uuid.uuid4(),
        name="Entra environment",
        start_url=f"{sites.app}/",
        login_url=f"{sites.app}/sso/start",
        sign_in_timeout_seconds=columns.pop("sign_in_timeout_seconds", 30),
        session_save_timeout_seconds=15,
        mfa_timeout_seconds=columns.pop("mfa_timeout_seconds", 60),
        **columns,
    )
    creds = {"username": USERNAME, "password": password}
    if totp_secret:
        creds["totp_secret"] = totp_secret
    profiles.write_credentials(profile, **creds)
    return profile


def _saved(updates: list[dict]) -> dict:
    saves = [u for u in updates if u.get("storage_state") is not None]
    assert saves, f"no session was saved: {updates}"
    return saves[-1]


def _cookie_hosts(state: dict) -> set[str]:
    return {str(c.get("domain") or "").lstrip(".") for c in state.get("cookies") or []}


def _decisions(outcome: signin.SignInOutcome) -> list[str]:
    return [step["decision"] for step in (outcome.trace or [])]


async def _final(cid: str, timeout: float = 90.0) -> signin.SignInOutcome:
    state = await signin._wait_for_change(
        cid, not_status={signin.STATUS_RUNNING, signin.STATUS_MFA_REQUIRED}, timeout=timeout
    )
    assert state is not None
    return state


@pytest.fixture(autouse=True)
async def _no_leftover_sign_in():
    yield
    tasks = list(signin._running.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    signin._running.clear()


# ── The passkey bridge is bypassed and the code method is used ────────────


async def test_a_passkey_account_signs_in_through_the_code_method_with_a_totp_secret(sites, updates):
    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=120)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    assert outcome.probe and outcome.probe["authenticated"] and outcome.probe["url"] == f"{sites.app}/"
    assert _cookie_hosts(_saved(updates)["storage_state"]) >= {"app.localhost", "idp.localhost"}
    assert len(sites.codes) == 1 and sites.code_ok(sites.codes[0])
    # The checkbox "don't ask for 30 days" was ticked and "Yes" (not "No") was clicked.
    assert sites.stay == [{"dontask": "1", "choice": "Yes"}]
    decisions = _decisions(outcome)
    assert "fido_bridge_detected" in decisions
    assert any(d.startswith("fido_bridge_bypassed") for d in decisions)
    assert "picker_choice=PhoneAppOTP" in decisions
    assert any(d.startswith("totp_submitted") for d in decisions)
    assert "kmsi_accepted" in decisions


async def test_the_bridge_is_bypassed_by_sign_in_another_way_when_there_is_no_cancel_url(sites, updates):
    sites.bridge_cancel_url = False
    sites.bridge_by_path = False  # detected by the blocked WebAuthn attempt, not the path

    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=120)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    decisions = _decisions(outcome)
    assert any("signInAnotherWay" in d for d in decisions)
    assert "picker_choice=PhoneAppOTP" in decisions


async def test_a_refused_code_is_retried_once_on_the_next_period(sites, updates, monkeypatch):
    waited: list[int] = []

    async def next_step(secret: str, counter: int) -> None:
        waited.append(counter)

    monkeypatch.setattr(signin, "_wait_for_step", next_step)
    sites.reject_codes = 1

    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=120)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    assert len(sites.codes) == 2 and sites.codes[0] != sites.codes[1]


async def test_a_picker_without_a_code_method_falls_to_a_push_challenge(sites, updates):
    sites.code_method = False

    outcome = await signin.start_sign_in(_profile(sites), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_MFA_REQUIRED and outcome.kind == signin.KIND_PUSH, outcome
    # The picker chose the only method it had - the push - and said so in the trace.
    live = signin._local_states.get(outcome.challenge_id or "")
    assert live is not None


# ── A "Choose a way to sign in" picker: the password, then a code the person types ──


async def test_the_method_picker_takes_the_password_then_asks_the_person_for_the_code(
    sites: Sites, updates: list[dict[str, object]]
) -> None:
    sites.method_picker = True

    outcome = await signin.start_sign_in(_profile(sites, totp_secret=None), user_id=None, wait_seconds=90)

    assert outcome.status == signin.STATUS_MFA_REQUIRED and outcome.kind == signin.KIND_CODE, outcome
    cid = outcome.challenge_id
    assert cid is not None
    code = totp.code_for_counter(SECRET, totp.current_counter(SECRET))
    answered = await signin.submit_code(cid, code, wait_seconds=60)
    final = answered if answered.status != signin.STATUS_RUNNING else await _final(cid)

    assert final.status == signin.STATUS_ACTIVE, final
    assert sites.codes == [code]
    decisions = _decisions(final)
    assert "picker_choice=password" in decisions
    assert "password_submitted" in decisions
    assert "fido_bridge_detected" not in decisions  # the passkey row was never chosen


async def test_a_method_picker_without_a_usable_method_fails_naming_it_never_a_push(
    sites: Sites, updates: list[dict[str, object]]
) -> None:
    sites.method_picker = True
    sites.picker_password = False

    outcome = await signin.start_sign_in(_profile(sites, totp_secret=None), user_id=None, wait_seconds=90)
    if outcome.status == signin.STATUS_RUNNING:
        assert outcome.challenge_id is not None
        outcome = await _final(outcome.challenge_id)

    assert outcome.status == signin.STATUS_FAILED, outcome
    assert outcome.stage == signin.STAGE_MFA
    assert "method picker" in outcome.message
    assert "unusable_method_picker" in _decisions(outcome)


async def test_a_waiting_challenge_is_answered_as_it_stands_unless_restarted(
    sites: Sites, updates: list[dict[str, object]]
) -> None:
    sites.method_picker = True
    profile = _profile(sites, totp_secret=None)

    first = await signin.start_sign_in(profile, user_id=None, wait_seconds=90)
    assert first.status == signin.STATUS_MFA_REQUIRED and first.kind == signin.KIND_CODE, first
    profile.mfa_challenge_id = first.challenge_id
    profile.session_status = SESSION_AWAITING_MFA

    again = await signin.start_sign_in(profile, user_id=None, wait_seconds=5)
    assert again.challenge_id == first.challenge_id

    restarted = await signin.start_sign_in(profile, user_id=None, wait_seconds=90, restart=True)
    assert first.challenge_id is not None and restarted.challenge_id != first.challenge_id, restarted
    assert first.challenge_id not in signin._running
    closed = await signin.read_state(first.challenge_id)
    assert closed is not None and closed.status == signin.STATUS_FAILED

    # A login setting changed since the challenge opened restarts it too.
    profile.mfa_challenge_id = restarted.challenge_id
    profile.login_config = {"confirm_selector": "#idSIButton9"}
    changed = await signin.start_sign_in(profile, user_id=None, wait_seconds=90)
    assert changed.challenge_id not in (first.challenge_id, restarted.challenge_id), changed


# ── The password detector only cries "refused" on the error banner ────────


async def test_a_wrong_password_is_refused_only_on_the_error_banner(sites, updates):
    outcome = await signin.start_sign_in(_profile(sites, password="not the password"), user_id=None, wait_seconds=60)
    if outcome.status == signin.STATUS_RUNNING:
        outcome = await _final(outcome.challenge_id)

    assert outcome.status == signin.STATUS_FAILED, outcome
    assert outcome.stage == signin.STAGE_IDP_FORM
    assert "incorrect" in outcome.message.lower()
    assert "credentials_refused" in _decisions(outcome)
    assert not [u for u in updates if u.get("storage_state") is not None]  # nothing saved


# ── A dry run reaches the application but saves nothing ────────────────────


async def test_a_dry_run_signs_in_end_to_end_and_saves_nothing(sites, updates):
    outcome = await signin.dry_run_sign_in(_profile(sites), user_id=None)

    assert outcome.status == signin.STATUS_ACTIVE, outcome
    assert outcome.trace and "picker_choice=PhoneAppOTP" in _decisions(outcome)
    # A dry run never writes the session, the status or the message: only what it learnt of the sign-in.
    assert [{k: v for k, v in u.items() if k != "profile_id"} for u in updates] == [{"signs_in_unattended": True}]
