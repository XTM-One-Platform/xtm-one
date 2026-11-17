"""A browser profile's session is what a probe found, never what its cookies suggest.

Without a browser: the status a saved session reads (``unverified`` until a
probe, then ``active`` or ``expired`` with where it landed), the per-profile
timeouts, what a profile may renew by itself, how a timeout and a refused
recording read, the ``sessionStorage`` a saved state carries beside
Playwright's, the TOTP time steps a retry moves to, how two reads of a page are
compared, and the tool contracts. The flows themselves run in a real Chromium
in ``test_browser_profile_signin_chromium.py``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from app.core import totp
from app.models.browser_profile import SESSION_ACTIVE, SESSION_EXPIRED, SESSION_NONE, SESSION_UNVERIFIED, BrowserProfile
from app.services.browser_profiles import browser, profiles, signin
from app.services.builtin_tools import video_studio as tool
from app.services.video_studio import capture

_STATE = {"cookies": [{"name": "sid", "value": "v", "domain": "app.example.test", "path": "/"}], "origins": []}
_PASSED = {"authenticated": True, "url": "https://app.example.test/home", "reason": "start_url renders the application"}
_FAILED = {"authenticated": False, "url": "https://app.example.test/login", "reason": "a sign-in form"}


def _profile(**columns) -> BrowserProfile:
    return BrowserProfile(id=uuid.uuid4(), name="Demo", start_url="https://app.example.test/", **columns)


def _cookie(domain: str) -> dict:
    return {"name": "c", "value": "v", "domain": domain, "path": "/"}


# ── C: the status is the probe's ──────────────────────────────────────────


def test_a_saved_session_without_a_probe_is_unverified_never_signed_in():
    profile = _profile()
    profiles.save_storage_state(profile, _STATE)

    assert profile.session_status == SESSION_UNVERIFIED
    assert profiles.serialize_profile(profile, can_manage=True)["session_status"] == SESSION_UNVERIFIED


def test_a_session_called_active_before_any_probe_reads_unverified():
    profile = _profile()
    profiles.save_storage_state(profile, _STATE)
    profile.session_status = SESSION_ACTIVE  # a row written when cookies were enough

    assert profiles.effective_session_status(profile) == SESSION_UNVERIFIED


def test_the_status_follows_the_probe():
    profile = _profile()
    profiles.save_storage_state(profile, _STATE, probe=_PASSED)
    payload = profiles.serialize_profile(profile, can_manage=True)
    assert payload["session_status"] == SESSION_ACTIVE
    assert payload["last_probe_at"] and payload["last_probe_result"]["url"] == _PASSED["url"]

    profiles.record_probe(profile, _FAILED)
    payload = profiles.serialize_profile(profile, can_manage=True)
    assert payload["session_status"] == SESSION_EXPIRED
    assert payload["last_probe_result"] == {
        **_FAILED,
        "http_status": None,
        "checked_at": payload["last_probe_result"]["checked_at"],
    }


def test_a_probe_the_application_could_not_answer_leaves_the_session_unverified_never_expired():
    profile = _profile()
    profiles.save_storage_state(profile, _STATE, probe=_PASSED)

    profiles.record_probe(profile, {**_FAILED, "url": "https://app.example.test/", "http_status": 503})

    assert profile.session_status == SESSION_UNVERIFIED
    assert profile.last_probe_result["http_status"] == 503
    assert profiles.serialize_profile(profile, can_manage=True)["session_status"] == SESSION_UNVERIFIED


_UNAVAILABLE = signin.ProbeResult(
    False, "https://app.example.test/", signin._unavailable_reason(503, "start_url"), http_status=503
)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        (None, None),
        ("", None),
        ("  12 ", 12.0),
        ("0", 0.0),
        ("-1", None),
        ("86400", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("nan", None),
    ],
)
def test_a_retry_after_is_read_in_seconds_and_bounded(header, seconds):
    headers = {} if header is None else {"retry-after": header}
    assert signin._retry_after(headers) == seconds


@pytest.mark.parametrize(
    ("attempt", "retry_after", "wait"),
    [(0, None, 3.0), (0, 10.0, 10.0), (1, 2.0, 8.0), (2, None, 15.0), (0, 120.0, 30.0), (2, 29.0, 29.0)],
)
def test_the_wait_before_a_retry_is_the_backoff_or_a_longer_retry_after_capped(attempt, retry_after, wait):
    assert signin._retry_wait(attempt, retry_after) == wait


async def test_an_unavailable_application_is_not_signed_in_again(monkeypatch):
    profile = _profile()
    profiles.write_credentials(profile, username="demo.user@example.test", password="pw")
    snap = profiles.snapshot(profile)
    assert snap.renewable
    refused = signin.ProbeResult(
        False, "https://app.example.test/", signin._unavailable_reason(503, "start_url"), http_status=503
    )
    sign_ins: list[object] = []

    async def probe_in(rb, snap):
        return refused

    async def drive_sign_in(*args, **kwargs):
        sign_ins.append(kwargs)
        raise AssertionError("an unavailable application must not be signed in again")

    monkeypatch.setattr(signin, "probe_in", probe_in)
    monkeypatch.setattr(signin, "drive_sign_in", drive_sign_in)

    with pytest.raises(signin.SessionNotSignedInError) as caught:
        await signin.ensure_signed_in(object(), snap)

    assert sign_ins == []
    assert caught.value.probe is refused
    assert str(caught.value) == f"profile session not checked: landed on https://app.example.test/ ({refused.reason})"
    assert str(capture.ProfileNotAuthenticatedError(refused)) == str(caught.value)


def test_a_probe_of_no_saved_session_is_no_session():
    profile = _profile()
    profiles.record_probe(profile, _PASSED)
    assert profile.session_status == SESSION_NONE


def test_forgetting_the_session_forgets_its_probe():
    profile = _profile()
    profiles.save_storage_state(profile, _STATE, probe=_PASSED)
    profiles.save_storage_state(profile, None)
    assert profile.session_status == SESSION_NONE
    assert profile.last_probe_at is None and profile.last_probe_result is None


# ── E: the timeouts are the profile's ─────────────────────────────────────


def test_the_timeouts_have_their_defaults_and_bounds():
    profile = _profile()
    payload = profiles.serialize_profile(profile, can_manage=True)
    assert (payload["sign_in_timeout_seconds"], payload["session_save_timeout_seconds"]) == (120, 30)
    assert payload["mfa_timeout_seconds"] == 300
    snap = profiles.snapshot(profile)
    assert (snap.sign_in_timeout, snap.session_save_timeout, snap.mfa_timeout) == (120.0, 30.0, 300.0)

    fields = profiles.clean_fields(
        name="Demo",
        start_url="https://app.example.test/",
        timeouts={"sign_in_timeout_seconds": 200, "mfa_timeout_seconds": None},
    )
    profile.mfa_timeout_seconds = 600
    profiles.apply_fields(profile, fields)
    assert profile.sign_in_timeout_seconds == 200
    assert profile.mfa_timeout_seconds == 600  # None keeps it


@pytest.mark.parametrize(
    "timeouts",
    [
        {"sign_in_timeout_seconds": 10},
        {"session_save_timeout_seconds": 500},
        {"mfa_timeout_seconds": "soon"},
        {"page_timeout_seconds": 30},
    ],
)
def test_a_timeout_out_of_bounds_is_refused(timeouts):
    with pytest.raises(profiles.ProfileError):
        profiles.clean_fields(name="Demo", start_url="https://app.example.test/", timeouts=timeouts)


def test_a_timeout_names_its_stage_and_the_last_url():
    error = signin.SignInTimeoutError(signin.STAGE_REDIRECT_CHAIN, "https://idp.example.test/relay", 120)

    assert error.stage == "redirect_chain" and error.last_url == "https://idp.example.test/relay"
    assert "redirect_chain" in str(error) and "https://idp.example.test/relay" in str(error) and "120 s" in str(error)
    assert set(signin.STAGES) == {"idp_form", "mfa", "redirect_chain", "app_load", "session_save"}


# ── D: what a recording may renew by itself, and how it refuses ───────────


def test_what_a_profile_can_renew_by_itself():
    with_credentials = _profile()
    profiles.write_credentials(with_credentials, username="demo.user@example.test", password="pw")
    assert profiles.snapshot(with_credentials).renewable

    replay = _profile(login_url="https://app.example.test/sso/start")
    profiles.save_storage_state(replay, {"cookies": [_cookie("idp.example.net")], "origins": []})
    assert profiles.snapshot(replay).holds_idp_session and profiles.snapshot(replay).renewable

    nowhere_to_replay = _profile()
    profiles.save_storage_state(nowhere_to_replay, {"cookies": [_cookie("idp.example.net")], "origins": []})
    assert not profiles.snapshot(nowhere_to_replay).renewable

    own_cookies_only = _profile(login_url="https://app.example.test/sso/start")
    profiles.save_storage_state(
        own_cookies_only, {"cookies": [_cookie("app.example.test"), _cookie(".example.test")], "origins": []}
    )
    assert not profiles.snapshot(own_cookies_only).holds_idp_session


def test_a_profile_of_a_public_site_needs_no_sign_in():
    assert not profiles.snapshot(_profile()).needs_sign_in
    assert profiles.snapshot(_profile(login_url="https://app.example.test/login")).needs_sign_in
    assert profiles.snapshot(_profile(login_config={"signed_in_selector": "#avatar"})).needs_sign_in


def test_a_refused_recording_says_where_the_profile_landed():
    probe = signin.ProbeResult(False, "https://app.example.test/login", "a sign-in form")
    error = capture.ProfileNotAuthenticatedError(probe, "The sign-in asks for a one-time code")

    assert isinstance(error, capture.TakeError)
    assert str(error) == (
        "profile not authenticated: landed on https://app.example.test/login (a sign-in form); "
        "signing in again failed: The sign-in asks for a one-time code"
    )


# ── B: what a saved session carries ───────────────────────────────────────


def test_session_storage_travels_beside_playwrights_state():
    stored = {"cookies": [], "origins": [], browser.SESSION_STORAGE_KEY: {"https://app.example.test": {"k": "v"}}}

    playwright_state, session = browser.split_storage_state(stored)

    assert playwright_state == {"cookies": [], "origins": []}
    assert session == {"https://app.example.test": {"k": "v"}}
    assert browser.split_storage_state(None) == (None, {})
    script = browser._session_storage_script(session)
    assert '"https://app.example.test"' in script and "sessionStorage.getItem(k) === null" in script


def test_the_page_s_local_storage_fills_what_playwrights_read_left_out():
    app = "https://app.example.test"
    other = {"origin": "https://idp.example.test", "localStorage": [{"name": "x", "value": "1"}]}

    added = browser.with_local_storage({"cookies": [], "origins": [other]}, app, {"copilot_token": "t"})
    assert added["origins"] == [other, {"origin": app, "localStorage": [{"name": "copilot_token", "value": "t"}]}]

    read = {"cookies": [], "origins": [{"origin": app, "localStorage": [{"name": "theme", "value": "dark"}]}]}
    merged = browser.with_local_storage(read, app, {"theme": "light", "copilot_token": "t"})
    assert merged["origins"][0]["localStorage"] == [
        {"name": "theme", "value": "dark"},  # Playwright's read wins
        {"name": "copilot_token", "value": "t"},
    ]
    assert browser.with_local_storage(read, app, {"theme": "dark"}) is read
    assert browser.local_storage_keys(merged, app + "/") == ["copilot_token", "theme"]


def test_the_persistence_note_names_cookies_and_key_names_never_values():
    state = {
        "cookies": [{"name": "sid", "value": "secret", "domain": ".example.test"}],
        "origins": [{"origin": "https://app.example.test", "localStorage": [{"name": "theme", "value": "secret"}]}],
    }
    note = browser.persistence_note(state, "https://app.example.test/home", page_keys=["copilot_token", "theme"])
    assert note == (
        "session not persisted (1 cookie(s) of app.example.test; localStorage keys: theme; held on the page but not "
        "saved: copilot_token)"
    )
    assert "secret" not in note
    assert browser.persistence_note(None, "https://app.example.test/") == (
        "session not persisted (no cookie of app.example.test; no localStorage key)"
    )


def test_a_session_in_local_storage_alone_is_said_saved_without_any_cookie():
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://app.example.test",
                "localStorage": [{"name": "app_token", "value": "secret"}, {"name": "theme", "value": "dark"}],
            }
        ],
    }
    note = browser.persistence_note(state, "https://app.example.test/", page_keys=["app_token", "theme"])
    assert note == (
        "session saved (no cookie of app.example.test; localStorage keys: app_token, theme), "
        "yet it does not reopen signed in"
    )
    assert "secret" not in note


def test_the_username_selectors_name_an_email_field_by_its_autocomplete():
    assert 'input[autocomplete~="email" i]' in signin.USERNAME_SELECTORS
    assert 'input[autocomplete~="username" i]' in signin.USERNAME_SELECTORS


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://App.Example.test:443/x?y=1", "https://app.example.test"),
        ("http://app.example.test:8080/", "http://app.example.test:8080"),
        ("not a url", ""),
    ],
)
def test_an_origin_is_spelled_as_the_browser_spells_it(url, origin):
    assert browser.origin_of(url) == origin


# ── A: a refused TOTP code moves to the next time step ────────────────────

_SECRET = base64.b32encode(b"neutral-test-key-0001").decode()


def test_the_totp_time_steps():
    assert totp.current_counter(_SECRET, now=59.0, min_remaining=0) == 1
    assert totp.code_for_counter(_SECRET, 1) == totp.current_code(_SECRET, now=59.0, min_remaining=0)
    # Less than 3 s left: the next step's code, which the server reads after the typing.
    assert totp.current_counter(_SECRET, now=58.5) == 2
    assert totp.seconds_until(_SECRET, 3, now=61.0) == 29.0
    assert totp.seconds_until(_SECRET, 1, now=61.0) == 0.0
    assert totp.code_for_counter(_SECRET, 2) != totp.code_for_counter(_SECRET, 3)


# ── How two reads of a page are compared ──────────────────────────────────


def test_a_page_is_its_origin_path_and_hash_route():
    assert signin._same_page("https://app.example.test/login?next=%2F", "https://app.example.test/login/")
    assert not signin._same_page("https://app.example.test/#/login", "https://app.example.test/#/home")
    assert signin._same_page("https://app.example.test/#top", "https://app.example.test/")
    assert not signin._same_page("https://app.example.test/login", "https://idp.example.net/login")


def test_the_signed_out_page_and_the_application_render_differently():
    signed_out = signin.PageState(url="u", title="Sign in", interactive=3, text_length=60)
    again = signin.PageState(url="u", title="Sign in", interactive=4, text_length=64)
    application = signin.PageState(url="u", title="Sign in", interactive=48, text_length=2400)

    assert signin._same_render(signed_out, again)
    assert not signin._same_render(signed_out, application)


def test_a_sign_in_form_is_never_the_signed_in_application():
    assert signin.PageState(url="u", password="[p]").sign_in_shaped
    assert signin.PageState(url="u", otp="[o]", otp_strong=True).sign_in_shaped
    assert signin.PageState(url="u", otp_boxes=6).sign_in_shaped
    # A short numeric field alone (a port, a quantity) is a code only during a sign-in.
    assert not signin.PageState(url="u", otp="[o]").sign_in_shaped


# ── The tools say what the sign-in does ───────────────────────────────────


def test_the_tools_describe_the_sign_in_contract():
    definitions = {d.name: d for d in tool._definitions(["high"])}

    sign_in = definitions["sign_in_browser_profile"].description
    for term in ("mfa_required", "challenge_id", "push_number", "submit_browser_mfa_code", "redirect_chain"):
        assert term in sign_in
    assert "profile's own context" in definitions["submit_browser_mfa_code"].description
    assert "never the cookies" in definitions["list_browser_profiles"].description
    assert "profile not authenticated" in definitions["record_browser_demo"].description
    check = definitions["check_browser_profile_session"]
    assert check.live_state and "session_check" in check.description
    dry_run = definitions["test_browser_profile_sign_in"]
    assert "dry" in dry_run.description.lower() and "saving" in dry_run.description.lower()


# ── TOTP secret validation (base32, otpauth, and what is refused) ─────────


@pytest.mark.parametrize(
    "value",
    [
        _SECRET,
        f"  {_SECRET[:4]} {_SECRET[4:8]} {_SECRET[8:]}  ",  # spaces accepted
        _SECRET.lower(),  # lowercase accepted
        f"otpauth://totp/Filigran:demo?secret={_SECRET}&digits=6&period=30&algorithm=SHA1",
    ],
)
def test_a_real_totp_secret_is_accepted(value):
    totp.validate_secret(value)
    assert totp.secret_status(value) == totp.SECRET_VALID


@pytest.mark.parametrize(
    "value",
    [
        "op://Private/Entra/one-time password",
        "vault://secret/data/entra#totp",
        "${TOTP_SECRET}",
        "{{ secrets.totp }}",
        "awssecret://prod/entra-totp",
    ],
)
def test_a_secret_manager_reference_is_refused_by_shape(value):
    with pytest.raises(totp.TOTPSecretError) as caught:
        totp.validate_secret(value)
    assert "secret-manager reference" in str(caught.value)
    assert totp.secret_status(value) == totp.SECRET_INVALID


@pytest.mark.parametrize("value", ["not a secret!!!", "hello world", "12345678", ""])
def test_a_value_that_is_not_a_key_is_refused(value):
    with pytest.raises(totp.TOTPSecretError):
        totp.validate_secret(value)
    assert totp.secret_status(value) == (totp.SECRET_NONE if not value else totp.SECRET_INVALID)


def test_saving_a_secret_manager_reference_is_refused_with_its_shape():
    profile = _profile()
    with pytest.raises(profiles.ProfileError) as caught:
        profiles.write_credentials(profile, totp_secret="op://Private/Entra/totp")
    assert "secret-manager reference" in str(caught.value)


# ── The TOTP status flows into the profile ────────────────────────────────


def test_the_profile_reports_its_totp_secret_status_and_whether_it_renews_itself():
    none = _profile()
    assert profiles.totp_secret_status(none) == "none"
    assert profiles.renews_by_itself(none) is False

    valid = _profile()
    profiles.write_credentials(valid, username="demo@example.test", password="pw", totp_secret=_SECRET)
    assert profiles.totp_secret_status(valid) == "valid"
    assert profiles.renews_by_itself(valid) is True
    assert profiles.snapshot(valid).has_valid_totp and profiles.snapshot(valid).renews_unattended
    payload = profiles.serialize_profile(valid, can_manage=True)
    assert payload["totp_secret_status"] == "valid" and payload["renews_by_itself"] is True

    # A secret stored before validation existed (a reference) reads "invalid" and never renews itself.
    import json as _json

    from app.core.security import encrypt_value

    broken = _profile()
    broken.credentials_enc = encrypt_value(
        _json.dumps({"username": "u", "password": "p", "totp_secret": "op://x/y"})
    )
    assert profiles.totp_secret_status(broken) == "invalid"
    assert profiles.renews_by_itself(broken) is False
    assert profiles.serialize_profile(broken, can_manage=True)["totp_secret_status"] == "invalid"


# ── The FIDO / passkey bridge is read by structure, never waited on as a push ──


def test_a_fido_bridge_is_detected_by_its_path_and_its_cancel_url_is_read():
    state = signin.PageState(
        url="https://login.microsoft.com/tenant-id/bridge/fido?cancelUrl=https%3A%2F%2Flogin.microsoftonline.com"
        "%2Fcommon%2Freprocess%3Fctx%3Dabc"
    )
    is_bridge, cancel = signin._fido_bridge(state)
    assert is_bridge
    assert cancel == "https://login.microsoftonline.com/common/reprocess?ctx=abc"


def test_a_fieldless_page_that_tried_webauthn_is_a_bridge_even_without_the_path():
    state = signin.PageState(url="https://login.microsoft.com/me/passkey", webauthn_attempts=1)
    assert signin._fido_bridge(state) == (True, "")


def test_a_bridge_without_a_cancel_url_reads_empty_not_none():
    state = signin.PageState(url="https://login.microsoft.com/t/bridge/fido")
    assert signin._fido_bridge(state) == (True, "")


@pytest.mark.parametrize(
    "state",
    [
        signin.PageState(url="https://login.microsoft.com/t/bridge/fido", password="[p]"),
        signin.PageState(url="https://login.microsoft.com/t/bridge/fido", otp="[o]", otp_strong=True),
        signin.PageState(url="https://idp.example/home"),
        signin.PageState(url="https://idp.example/relay", relay="https://app.example/acs"),
    ],
)
def test_a_page_with_a_field_a_relay_or_no_signal_is_not_a_bridge(state):
    assert signin._fido_bridge(state) == (False, None)


def test_the_totp_refused_stage_is_a_failure_stage_not_a_timeout_stage():
    assert signin.STAGE_MFA_TOTP_REFUSED == "mfa_totp_refused"
    # It is deliberately NOT a timeout stage, so STAGES (the timeout vocabulary) is unchanged.
    assert signin.STAGE_MFA_TOTP_REFUSED not in signin.STAGES


def test_the_sign_in_state_schema_carries_the_trace():
    from app.api.v1.schemas.browser_profiles import SignInState

    state = SignInState(
        status="failed",
        message="stalled",
        trace=[{"stage": "mfa", "url": "https://idp/x", "decision": "fido_bridge_bypassed", "at": "t"}],
    )
    assert state.trace[0]["decision"] == "fido_bridge_bypassed"


def test_the_profile_list_reads_the_probe():
    expired = {
        "session_status": SESSION_EXPIRED,
        "last_probe_result": _FAILED,
        "last_probe_at": "2026-10-18T10:00:00+00:00",
        "mfa_challenge": None,
    }
    assert tool._session_text(expired) == f"session expired: start_url lands on {_FAILED['url']} (a sign-in form)"
    waiting = {
        "session_status": "awaiting_mfa",
        "mfa_challenge": {"kind": "code", "challenge_id": "c1", "expires_at": "t"},
    }
    assert "challenge_id `c1`" in tool._session_text(waiting)
    assert "not checked" in tool._session_text({"session_status": SESSION_UNVERIFIED, "mfa_challenge": None})


# ── Every consumer of an unavailable probe: kept unverified, never signed in again ──


class _Db:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, _model, _id):
        return self.rows[0] if self.rows else None

    async def commit(self):
        self.commits += 1

    async def refresh(self, _row):
        return None


@contextlib.asynccontextmanager
async def _no_browser(**_kwargs):
    yield SimpleNamespace()


def _renewable_profile() -> BrowserProfile:
    """Credentials and a valid TOTP secret: a profile that renews unattended."""
    profile = _profile(keepalive_minutes=60)
    secret = base64.b32encode(b"neutral-test-key-0001").decode()
    profiles.write_credentials(profile, username="demo.user@example.test", password="pw", totp_secret=secret)
    profiles.save_storage_state(profile, _STATE, probe=_PASSED)
    profile.last_probe_at = None
    return profile


def _keepalive_fakes(monkeypatch, profile, *, probe, saved=None):
    from app.core import redis as redis_module
    from app.services.browser_profiles import browser as browser_module
    from app.services.browser_profiles import keepalive

    class _Redis:
        async def set(self, *_args, **_kwargs):
            return True

        async def delete(self, *_args):
            return 1

    async def client():
        return _Redis()

    async def probe_in(rb, snap):
        return probe

    async def drive_sign_in(*_args, **_kwargs):
        raise AssertionError("an unavailable application must not be signed in again")

    async def save_session(rb, snap):
        assert saved is not None, "no session is read from a probe that did not pass"
        return saved

    monkeypatch.setattr(redis_module, "redis_client", client)
    monkeypatch.setattr(keepalive, "async_session", lambda: _Db([profile]))
    monkeypatch.setattr(browser_module, "profile_browser", _no_browser)
    monkeypatch.setattr(signin, "probe_in", probe_in)
    monkeypatch.setattr(signin, "drive_sign_in", drive_sign_in)
    monkeypatch.setattr(signin, "save_session", save_session)
    return keepalive


async def test_the_keepalive_does_not_sign_an_unavailable_application_in_again(monkeypatch):
    profile = _renewable_profile()
    saved_state = profile.storage_state_enc
    keepalive = _keepalive_fakes(monkeypatch, profile, probe=_UNAVAILABLE)

    await keepalive.keepalive_sweep()

    assert profile.session_status == SESSION_UNVERIFIED
    assert profile.storage_state_enc == saved_state
    assert profile.last_probe_result["http_status"] == 503
    assert profile.last_sign_in_message == (
        f"The keep-alive could not check the session: start_url lands on https://app.example.test/ "
        f"({_UNAVAILABLE.reason})."
    )


async def test_the_keepalive_keeps_a_refreshed_session_whose_re_probe_was_refused(monkeypatch):
    profile = _renewable_profile()
    refreshed = {"cookies": [{"name": "sid", "value": "refreshed", "domain": "app.example.test", "path": "/"}]}
    passed = signin.ProbeResult(True, "https://app.example.test/", "start_url renders the application")
    keepalive = _keepalive_fakes(monkeypatch, profile, probe=passed, saved=(refreshed, _UNAVAILABLE))

    await keepalive.keepalive_sweep()

    assert profiles.snapshot(profile).storage_state == refreshed
    assert profile.session_status == SESSION_UNVERIFIED
    assert profile.last_probe_result["http_status"] == 503


async def test_the_session_check_route_does_not_sign_an_unavailable_application_in_again(monkeypatch):
    from app.api.v1 import browser_profiles as route

    profile = _renewable_profile()
    assert profiles.snapshot(profile).renews_unattended
    stored: list = []
    started: list = []

    async def usable(db, profile_id, user):
        return profile

    async def nothing(*_args, **_kwargs):
        return None

    async def probe_profile(snap):
        return _UNAVAILABLE

    async def store_probe(profile_id, probe, *, message=None):
        stored.append((profile_id, probe))

    async def start_sign_in(*args, **kwargs):
        started.append(kwargs)

    async def groups(*_args):
        return []

    monkeypatch.setattr(route, "_usable_profile", usable)
    monkeypatch.setattr(route, "log_audit", nothing)
    monkeypatch.setattr(route, "get_user_group_ids", groups)
    monkeypatch.setattr(route, "can_manage", lambda *_args: True)
    monkeypatch.setattr(route, "serialize_profile", lambda p, can_manage: {"id": str(p.id)})
    monkeypatch.setattr(signin, "probe_profile", probe_profile)
    monkeypatch.setattr(signin, "store_probe", store_probe)
    monkeypatch.setattr(signin, "start_sign_in", start_sign_in)

    answer = await route.check_profile_session(str(profile.id), user=SimpleNamespace(id=uuid.uuid4()), db=_Db())

    assert answer == {"id": str(profile.id), "vault_secrets": None}
    assert started == []
    assert stored == [(str(profile.id), _UNAVAILABLE)]


async def test_the_session_check_route_answers_in_bounded_time(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import browser_profiles as route

    profile = _renewable_profile()

    async def usable(db, profile_id, user):
        return profile

    async def nothing(*_args, **_kwargs):
        return None

    async def hangs(snap):
        await asyncio.sleep(30)

    monkeypatch.setattr(route, "_usable_profile", usable)
    monkeypatch.setattr(route, "log_audit", nothing)
    monkeypatch.setattr(route, "CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(signin, "probe_profile", hangs)

    with pytest.raises(HTTPException) as caught:
        await route.check_profile_session(str(profile.id), user=SimpleNamespace(id=uuid.uuid4()), db=_Db())

    assert caught.value.status_code == 503
    assert caught.value.detail == "The session check did not finish in time: try again."


async def test_the_session_check_tool_says_the_application_did_not_answer(monkeypatch):
    from app.models.user import User
    from app.tools.base import ToolCall

    profile = _renewable_profile()
    stored: list = []

    async def find_user(_db, _user_id):
        return User(id=uuid.uuid4(), email="demo.user@example.test")

    async def usable(_db, _profile_id, _user_id):
        return profile

    async def probe_profile(snap):
        return _UNAVAILABLE

    async def store_probe(profile_id, probe, *, message=None):
        stored.append(probe)

    async def start_sign_in(*_args, **_kwargs):
        raise AssertionError("an unavailable application must not be signed in again")

    monkeypatch.setattr(tool, "async_session", lambda: _Db())
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(profiles, "load_usable_profile", usable)
    monkeypatch.setattr(signin, "probe_profile", probe_profile)
    monkeypatch.setattr(signin, "store_probe", store_probe)
    monkeypatch.setattr(signin, "start_sign_in", start_sign_in)
    _definitions, executor = tool.build_video_studio_tools(None, str(uuid.uuid4()))

    result = await executor.execute_tool(
        ToolCall(id="c1", name="check_browser_profile_session", arguments={"profile_id": str(profile.id)})
    )

    text, _, payload = result.content.partition("\n")
    assert text == (
        f"Could not check: start_url answered HTTP 503 ({_UNAVAILABLE.reason}). The saved session is untouched and "
        "was not signed in again: do not sign the profile in, check again later."
    )
    assert json.loads(payload)["session_check"]["http_status"] == 503
    assert stored == [_UNAVAILABLE]


async def test_an_agent_session_on_an_unavailable_application_is_not_sent_to_sign_in(monkeypatch):
    from app.services.browser_profiles import agent_sessions
    from app.services.browser_profiles import browser as browser_module

    profile = _renewable_profile()
    stored: list = []

    async def ensure_signed_in(rb, snap, **_kwargs):
        raise signin.SessionNotSignedInError(_UNAVAILABLE)

    async def store_probe(profile_id, probe, *, message=None):
        stored.append((probe, message))

    monkeypatch.setattr(browser_module, "profile_browser", _no_browser)
    monkeypatch.setattr(signin, "ensure_signed_in", ensure_signed_in)
    monkeypatch.setattr(signin, "store_probe", store_probe)
    access = SimpleNamespace(is_read_only=lambda _id: False)

    with pytest.raises(agent_sessions.ProfileSessionError) as caught:
        await agent_sessions.open_profile_session(profile, access=access)

    error = signin.session_error_text(_UNAVAILABLE)
    assert str(caught.value) == (
        f"The browser profile 'Demo' could not be checked ({error}). {agent_sessions.UNAVAILABLE_HINT}"
    )
    assert stored == [(_UNAVAILABLE, f"An agent could not check the session: {error}")]


# ── The edges of an unavailable application ──────────────────────


async def test_judge_hands_the_probe_deadline_to_the_signed_out_render(monkeypatch):
    snap = profiles.snapshot(_profile())
    deadline = time.monotonic() - 1.0  # already spent: a retry of the signed-out render must not be taken
    received: list = []

    async def signed_out_render(rb, snap, *, deadline=None):
        received.append(deadline)
        return None

    monkeypatch.setattr(signin, "signed_out_render", signed_out_render)

    probe = await signin._judge(
        object(), snap, object(), signin.PageState(url="https://app.example.test/home"), deadline=deadline
    )

    assert received == [deadline]
    assert probe == signin.ProbeResult(
        True, "https://app.example.test/home", "start_url renders the application", checked_at=probe.checked_at
    )


# ── A stale signed_in_selector (#10002) ─────────────────────────────────────


async def _no_signed_out_render(rb, snap, *, deadline=None):
    return None


async def test_a_stale_signed_in_selector_passes_a_page_that_renders_the_application(monkeypatch):
    monkeypatch.setattr(signin, "signed_out_render", _no_signed_out_render)
    snap = profiles.snapshot(_profile(login_config={"signed_in_selector": "#old-avatar"}))
    state = signin.PageState(url="https://app.example.test/", signed_in=False)

    probe = await signin._past_stale_selector(object(), snap, state)

    assert probe.authenticated
    assert probe.reason == (
        "start_url renders the application; the profile's signed_in_selector (#old-avatar) is not on it: "
        "stale, update or clear it"
    )


async def test_a_missing_signed_in_selector_on_a_sign_in_form_fails_naming_the_selector(monkeypatch):
    monkeypatch.setattr(signin, "signed_out_render", _no_signed_out_render)
    snap = profiles.snapshot(_profile(login_config={"signed_in_selector": "#avatar"}))
    state = signin.PageState(url="https://app.example.test/login", signed_in=False, password="[p]")

    probe = await signin._past_stale_selector(object(), snap, state)

    assert not probe.authenticated
    assert probe.reason == (
        "the profile's signed_in_selector (#avatar) is not on the page; "
        "a sign-in form (a password or one-time-code field)"
    )


def test_the_selector_verdict_names_the_selector_and_a_signed_out_url_stays_final():
    cfg = {"signed_in_selector": "[data-user-menu]", "signed_out_url_pattern": "/login"}
    page = signin.PageState(url="https://app.example.test/home", signed_in=False)
    assert signin._marked(page, cfg).reason == "the profile's signed_in_selector ([data-user-menu]) is not on the page"
    assert signin._selector_missing(page, cfg)
    assert not signin._selector_missing(signin.PageState(url="https://app.example.test/login", signed_in=False), cfg)
    assert not signin._selector_missing(signin.PageState(url="https://app.example.test/home", signed_in=True), cfg)
    assert signin._shown_selector({"signed_in_selector": "x" * 200}) == "x" * 117 + "..."


# ── Sign-in form and application at one URL (#10027) ──────────────────────────


async def _signed_out_form(rb, snap, *, deadline=None):
    return signin.PageState(
        url="https://app.example.test/", title="App", signature="user,password", interactive=3, text_length=40
    )


async def test_the_signed_in_application_at_the_login_url_of_its_start_url_is_signed_in(monkeypatch):
    # A single-page application serving its sign-in form and its dashboard at "/", its session a localStorage token.
    monkeypatch.setattr(signin, "signed_out_render", _signed_out_form)
    snap = profiles.snapshot(
        _profile(login_url="https://app.example.test/", login_config={"keep_storage_keys": ["app_token"]})
    )
    dashboard = signin.PageState(url="https://app.example.test/", title="App", interactive=60, text_length=4000)

    probe = await signin._judge_structure(object(), snap, dashboard, snap.login_config)

    assert probe.authenticated
    assert probe.reason == "start_url renders the application"


async def test_the_sign_in_form_at_the_shared_url_still_fails_naming_its_signal(monkeypatch):
    monkeypatch.setattr(signin, "signed_out_render", _signed_out_form)
    snap = profiles.snapshot(_profile(login_url="https://app.example.test/"))
    form = await _signed_out_form(None, snap)
    form.password = "[p]"
    assert (await signin._judge_structure(object(), snap, form, {})).reason == (
        "a sign-in form (a password or one-time-code field)"
    )
    form.password = None  # a fieldless read of the same render: the signed-out render comparison decides
    probe = await signin._judge_structure(object(), snap, form, {})
    assert not probe.authenticated
    assert "same URL, title, fields and amount of content as the signed-out render" in probe.reason


async def test_a_login_url_apart_from_start_url_stays_the_profile_s_sign_in_page(monkeypatch):
    monkeypatch.setattr(signin, "signed_out_render", _no_signed_out_render)
    snap = profiles.snapshot(_profile(login_url="https://app.example.test/login"))
    probe = await signin._judge_structure(object(), snap, signin.PageState(url="https://app.example.test/login"), {})
    assert not probe.authenticated
    assert probe.reason == "the profile's sign-in page (the URL is the profile's login_url)"


_EXPIRED = signin.ProbeResult(False, "https://app.example.test/login", "a sign-in form")
_SIGNED_IN = signin.ProbeResult(True, "https://app.example.test/home", "start_url renders the application")


def _sign_in_fakes(monkeypatch, probe):
    published: list = []
    updates: list = []

    @contextlib.asynccontextmanager
    async def browser(**_kwargs):
        yield SimpleNamespace(page=None)

    async def drive_sign_in(rb, snap, **_kwargs):
        return signin.SignInResult(ok=True, message="Signed in.")

    async def save_session(rb, snap):
        return _STATE, probe

    async def publish(cid, outcome):
        published.append(outcome)

    async def update_profile(profile_id, **changes):
        updates.append(changes)

    async def screenshot(page, user_id):
        return None

    monkeypatch.setattr(signin, "profile_browser", browser)
    monkeypatch.setattr(signin, "drive_sign_in", drive_sign_in)
    monkeypatch.setattr(signin, "save_session", save_session)
    monkeypatch.setattr(signin, "publish", publish)
    monkeypatch.setattr(signin, "_update_profile", update_profile)
    monkeypatch.setattr(signin, "_screenshot", screenshot)
    return published, updates


@pytest.mark.parametrize(
    ("probe", "status", "stage", "message"),
    [
        (
            _SIGNED_IN,
            signin.STATUS_ACTIVE,
            None,
            "Signed in. The session is saved; opened again, start_url renders the application.",
        ),
        (
            _UNAVAILABLE,
            signin.STATUS_UNVERIFIED,
            None,
            "Signed in. The session is saved, but it could not be checked: start_url lands on "
            f"https://app.example.test/ ({_UNAVAILABLE.reason}).",
        ),
        (
            _EXPIRED,
            signin.STATUS_FAILED,
            signin.STAGE_APP_LOAD,
            "The application rendered signed in, but the saved session does not open it again: start_url lands on "
            "https://app.example.test/login (a sign-in form).",
        ),
    ],
)
async def test_a_sign_in_ends_with_what_the_probe_of_its_saved_session_says(monkeypatch, probe, status, stage, message):
    published, updates = _sign_in_fakes(monkeypatch, probe)

    await signin._run_sign_in("cid-1", profiles.snapshot(_renewable_profile()), None)

    assert [outcome.status for outcome in published] == [signin.STATUS_RUNNING, status]
    final = published[-1]
    assert (final.stage, final.message, final.probe) == (stage, message, probe.to_dict())
    assert updates == [{"storage_state": _STATE, "probe": probe.to_dict(), "message": message}]


async def test_a_dry_run_whose_session_the_application_did_not_answer_is_unverified_never_failed(monkeypatch):
    published, updates = _sign_in_fakes(monkeypatch, _UNAVAILABLE)

    outcome = await signin.dry_run_sign_in(_renewable_profile(), user_id=None)

    assert (outcome.status, outcome.stage) == (signin.STATUS_UNVERIFIED, None)
    assert outcome.message == (
        "Dry run reached the application signed in, but the session it would save could not be checked: start_url "
        f"lands on https://app.example.test/ ({_UNAVAILABLE.reason}). Nothing was saved."
    )
    assert (published, updates) == ([], [])


@pytest.mark.parametrize(("after_probe", "left"), [(160.0, 30.0), (195.0, 0.0)])
async def test_the_session_check_route_bounds_the_renewal_by_what_the_probe_left(monkeypatch, after_probe, left):
    from app.api.v1 import browser_profiles as route

    profile = _renewable_profile()
    waits: list = []
    clock = iter([100.0, after_probe])

    async def usable(db, profile_id, user):
        return profile

    async def nothing(*_args, **_kwargs):
        return None

    async def probe_profile(snap):
        return _EXPIRED

    async def start_sign_in(_profile, *, user_id, wait_seconds):
        waits.append(wait_seconds)

    async def groups(*_args):
        return []

    monkeypatch.setattr(route, "_usable_profile", usable)
    monkeypatch.setattr(route, "log_audit", nothing)
    monkeypatch.setattr(route, "get_user_group_ids", groups)
    monkeypatch.setattr(route, "can_manage", lambda *_args: True)
    monkeypatch.setattr(route, "serialize_profile", lambda p, can_manage: {"id": str(p.id)})
    monkeypatch.setattr(route, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(signin, "probe_profile", probe_profile)
    monkeypatch.setattr(signin, "start_sign_in", start_sign_in)

    await route.check_profile_session(str(profile.id), user=SimpleNamespace(id=uuid.uuid4()), db=_Db())

    assert route.CHECK_TIMEOUT_SECONDS == 90.0
    assert waits == [left]
