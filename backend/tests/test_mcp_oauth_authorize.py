"""Regression + hardening tests for the MCP OAuth authorization endpoint
(``app.api.v1.mcp_oauth.authorize_post``).

The headline bug was an unhandled ``AttributeError`` — the handler
read ``user.password_hash`` which does not exist on the ``User`` model
(the field is ``hashed_password``), so every authorization attempt that
resolved a real account 500'd. The fix also brings the endpoint in
lockstep with the canonical local-login contract in ``auth.login`` so this
public, browser-facing, password-checking endpoint cannot drift from it:

* reads the real ``hashed_password`` field;
* SSO-only accounts (``hashed_password is None``) are rejected as invalid
  credentials instead of crashing;
* case-insensitive email resolution (legacy / SSO-provisioned mixed-case
  rows still match);
* the ``local_auth_enabled`` gate (SSO-only platforms refuse local
  passwords here);
* per-IP rate limiting runs *before* any DB work, so the endpoint cannot
  be used for credential stuffing / brute force;
* an account with two-factor enabled gets no code for its password alone:
  the password step answers with the code step, and the code is
  checked by ``auth.complete_mfa_challenge``, the check behind
  ``POST /auth/mfa/verify``.

A successful sign-in answers with a page that sends the browser on to the
client's ``redirect_uri``, not with a ``302``: browsers check the consent
page's ``form-action`` on every redirect the form submission follows, and an
MCP client's callback often redirects again.

These tests exercise the route function directly with lightweight stubs
(the codebase has no FastAPI test client / DB session fixture — see
``test_auth_security.py`` for the same approach).
"""

from __future__ import annotations

import ast
import re
import time
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.v1 import auth as auth_api
from app.api.v1 import mcp_oauth as mcp_oauth_module
from app.api.v1.mcp_oauth import authorize_post
from app.core import security
from app.services import mfa
from tests import app_sources

REDIRECT_URI = "https://app.example.com/callback"


class _StubDB:
    """Async DB stub that returns queued ``scalar_one_or_none`` results in
    call order and captures each statement's WHERE clause.

    Capturing the WHERE text lets a test assert on the *shape* of the
    lookup (e.g. ``lower(users.email)``) without a real database — the
    same technique used across ``test_auth_security.py``.
    """

    def __init__(self, results: list[object], *, user: object = None):
        self._results = list(results)
        self._user = user
        self.where_texts: list[str] = []
        self.execute_calls = 0
        self.commits = 0

    async def execute(self, stmt):
        self.execute_calls += 1
        try:
            self.where_texts.append(
                str(stmt.whereclause) if stmt.whereclause is not None else ""
            )
        except Exception:  # pragma: no cover - defensive only
            self.where_texts.append("")
        value = self._results.pop(0) if self._results else None

        class _R:
            def scalar_one_or_none(self):
                return value

        return _R()

    def add(self, _obj):  # pragma: no cover - log_audit is stubbed out
        pass

    async def flush(self):  # pragma: no cover - log_audit is stubbed out
        pass

    async def commit(self):
        self.commits += 1

    async def get(self, _model, _pk):
        # The code step loads the challenge's user by primary key.
        return self._user

    async def refresh(self, _obj, with_for_update=None):
        # ``auth._lock_mfa_state`` re-reads the user under a row lock.
        pass


def _make_client() -> SimpleNamespace:
    return SimpleNamespace(client_name="VS Code", redirect_uris=[REDIRECT_URI])


def _make_request(host: str = "127.0.0.1") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host), headers={})


def _patch_common(monkeypatch, *, local_auth_enabled: bool = True) -> list[dict]:
    """Patch every collaborator the route reaches after the credential
    check so tests run without Redis / branding / SSO config / audit DB
    writes. Returns the list that captures ``log_audit`` calls.
    """
    import app.core.sso_helpers as sso_module

    async def _no_rate_limit(*_a, **_kw):
        return None

    async def _branding(_db):
        return SimpleNamespace(name="TestApp")

    async def _sso(_db):
        return SimpleNamespace(local_auth_enabled=local_auth_enabled)

    async def _store_auth_code(*_a, **_kw):
        return None

    audit_calls: list[dict] = []

    async def _log_audit(*_a, **kw):
        audit_calls.append(kw)
        return None

    monkeypatch.setattr(mcp_oauth_module, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(mcp_oauth_module, "get_branding", _branding)
    monkeypatch.setattr(sso_module, "get_sso_config", _sso)
    monkeypatch.setattr(mcp_oauth_module, "_store_auth_code", _store_auth_code)
    monkeypatch.setattr(mcp_oauth_module, "log_audit", _log_audit)
    # The two-factor step runs the auth module's own helpers.
    monkeypatch.setattr(auth_api, "enforce_rate_limit", _no_rate_limit)
    monkeypatch.setattr(auth_api, "log_audit", _log_audit)
    return audit_calls


async def _call(
    db,
    *,
    email: str = "",
    password: str = "",
    state: str = "xyz",
    redirect_uri: str = REDIRECT_URI,
    mfa_token: str = "",
    code: str = "",
):
    # Every form field is passed: called directly, a route's ``Form("")``
    # default is the ``FieldInfo`` object, not the empty string.
    return await authorize_post(
        request=_make_request(),
        email=email,
        password=password,
        client_id="mcp-abc123",
        redirect_uri=redirect_uri,
        state=state,
        code_challenge="challenge",
        code_challenge_method="S256",
        response_type="code",
        resource="",
        mfa_token=mfa_token,
        code=code,
        db=db,
    )


class _ReturnPage(HTMLParser):
    """What the page a successful sign-in answers with does: the URL of its
    refresh and of its fallback link (attribute values unescaped, as the
    browser reads them), and whether it carries a script or a form."""

    def __init__(self) -> None:
        super().__init__()
        self.refresh: str | None = None
        self.links: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        attributes = dict(attrs)
        if tag == "meta" and (attributes.get("http-equiv") or "").lower() == "refresh":
            self.refresh = attributes.get("content")
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])


def _return_page(resp: HTMLResponse) -> tuple[str, _ReturnPage]:
    """The URL the page sends the browser to, and the parsed page."""
    page = _ReturnPage()
    page.feed(resp.body.decode())
    assert page.refresh is not None, "the page must navigate by a declarative refresh"
    delay, _, url = page.refresh.partition(";")
    assert delay.strip() == "0"
    url = url.strip()
    assert url[:4].lower() == "url="
    url = url[4:]
    assert page.links == [url], "the fallback link must lead where the refresh does"
    return url, page


def _patch_successful_sign_in(monkeypatch, password: str) -> SimpleNamespace:
    _patch_common(monkeypatch)
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="alice@example.com",
        full_name="Alice Example",
        is_active=True,
        hashed_password=security.hash_password(password),
    )


# ── regression: reads hashed_password, not password_hash ────────────────


async def test_authorize_post_authenticates_via_hashed_password(monkeypatch):
    """The headline fix: a user object exposing ``hashed_password`` (and
    NOT ``password_hash``) must authenticate without raising.

    The stub user deliberately has no ``password_hash`` attribute, so any
    regression that reverts to ``user.password_hash`` raises
    ``AttributeError`` here instead of the
    return to the client this test asserts.
    """
    audit_calls = _patch_common(monkeypatch)

    password = "Sup3rSecret1"
    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="alice@example.com",
        full_name="Alice Example",
        is_active=True,
        hashed_password=security.hash_password(password),
    )
    assert not hasattr(user, "password_hash"), (
        "the regression guard requires the stub user to lack the "
        "non-existent password_hash attribute"
    )

    db = _StubDB([_make_client(), user])
    resp = await _call(db, email="alice@example.com", password=password)

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    location, _ = _return_page(resp)
    assert location.startswith(REDIRECT_URI)
    assert "code=" in location
    assert "state=xyz" in location
    assert audit_calls, "a successful MCP OAuth authorization must be audited"
    assert audit_calls[0]["details"]["login_method"] == "mcp_oauth"


# ── the sign-in leaves by a page, not by a redirect ─────────────────────


async def test_authorize_post_sends_the_browser_on_by_a_page_not_a_redirect(monkeypatch):
    """Browsers check ``form-action`` on every redirect the form submission's
    navigation follows. A ``302`` to the client's callback, which redirects
    again to the client's own front end, met the consent page's
    ``form-action`` on that second hop and stopped once the policy was
    enforced. The answer is a page whose refresh starts a navigation that is
    not a form submission, sent where the ``302`` went: the registered
    ``redirect_uri`` with the code and the state appended, escaped the way
    ``Location`` was.
    """
    password = "Sup3rSecret1"
    user = _patch_successful_sign_in(monkeypatch, password)
    redirect_uri = "https://app.example.com/oauth/callback?next=/home&tenant=a'b"
    state = 'st"<ate>&x'
    client = SimpleNamespace(client_name="Claude <beta>", redirect_uris=[redirect_uri])

    db = _StubDB([client, user])
    resp = await _call(db, email="alice@example.com", password=password, state=state, redirect_uri=redirect_uri)

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "location" not in resp.headers
    # It carries a single-use authorization code: kept by no cache, as the token endpoint.
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"

    location, page = _return_page(resp)
    parts = urlsplit(location)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "app.example.com", "/oauth/callback")
    query = parse_qs(parts.query)
    assert query["next"] == ["/home"]
    assert query["tenant"] == ["a'b"]
    assert query["state"] == [state]
    assert len(query["code"]) == 1 and query["code"][0]
    # RFC 9207: the issuer travels with the code, the mix-up defence.
    assert query["iss"] == [mcp_oauth_module._base_url()]
    # Nothing the page needs from its policy beyond the consent page's own,
    # and nothing that could post anywhere.
    assert "script" not in page.tags
    assert "form" not in page.tags
    body = resp.body.decode()
    assert not re.search(r"\son[a-z]+\s*=", body, flags=re.IGNORECASE)
    # The client's name is text, never markup.
    assert "Claude <beta>" not in body
    assert "Claude &lt;beta&gt;" in body



@pytest.mark.parametrize(
    "url",
    [
        "https://a.example/cb?x=1&y=a b",
        "http://localhost:1/cb?q='\"<>",
        "https://a.example/\u00fc?x=%41#frag",
    ],
    ids=["space", "quotes-and-angles", "non-ascii-and-escape"],
)
def test_the_return_page_names_the_url_a_302_would(url):
    """The page is a ``302`` the browser follows by itself: it must send the
    browser to the very URL Starlette's ``RedirectResponse`` would have put in
    ``Location``. ``_REDIRECT_URL_SAFE`` copies Starlette's quoting; this pins
    the copy to it."""
    expected = RedirectResponse(url).headers["location"]
    resp = mcp_oauth_module._grant_response(url, client_name="c", app_name="a")
    location, _ = _return_page(resp)
    assert location == expected


def test_no_grant_of_the_authorize_endpoint_answers_with_a_redirect():
    """Every successful grant answers through ``_grant_response``: a
    ``RedirectResponse`` anywhere in the module - in a second-factor step,
    say - would bring the blocked second hop back for that path, and, the
    import being gone, fail with a ``NameError`` at run time instead of at
    review."""
    path = app_sources.APP_DIR / "api" / "v1" / "mcp_oauth.py"
    uses = [
        node.lineno
        for node in app_sources.walk(app_sources.tree(path))
        if (isinstance(node, ast.Name) and node.id == "RedirectResponse")
        or (isinstance(node, ast.Attribute) and node.attr == "RedirectResponse")
        or (isinstance(node, ast.alias) and node.name == "RedirectResponse")
    ]
    assert uses == [], (
        f"mcp_oauth.py uses RedirectResponse at lines {uses}: answer a grant with _grant_response()"
    )


async def test_authorize_post_sso_only_user_rejected_without_crash(monkeypatch):
    """SSO-only accounts have ``hashed_password is None``. The endpoint
    must reject them as invalid credentials (consent page, HTTP 200) and
    never crash.
    """
    _patch_common(monkeypatch)

    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="sso@example.com",
        full_name="SSO User",
        is_active=True,
        hashed_password=None,
    )
    db = _StubDB([_make_client(), user])
    resp = await _call(db, email="sso@example.com", password="anything")

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    assert "Invalid email or password" in resp.body.decode()


# ── credential checks mirror auth.login ─────────────────────────────────


async def test_authorize_post_wrong_password_rejected(monkeypatch):
    """A valid account + wrong password must land on the consent page with
    the generic error (no account enumeration), not a redirect.
    """
    _patch_common(monkeypatch)

    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="alice@example.com",
        full_name="Alice Example",
        is_active=True,
        hashed_password=security.hash_password("Correct1Password"),
    )
    db = _StubDB([_make_client(), user])
    resp = await _call(db, email="alice@example.com", password="WrongPassword1")

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    assert "Invalid email or password" in resp.body.decode()
    assert "cache-control" not in resp.headers


async def test_authorize_post_disabled_account_rejected(monkeypatch):
    """An inactive account with the right password must still be refused —
    mirrors the ``is_active`` gate in ``auth.login``.
    """
    _patch_common(monkeypatch)

    password = "Sup3rSecret1"
    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="alice@example.com",
        full_name="Alice Example",
        is_active=False,
        hashed_password=security.hash_password(password),
    )
    db = _StubDB([_make_client(), user])
    resp = await _call(db, email="alice@example.com", password=password)

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    assert "Your account is disabled" in resp.body.decode()


async def test_authorize_post_uses_case_insensitive_email_lookup(monkeypatch):
    """The user lookup must use ``func.lower(User.email)`` so an account
    stored with a mixed-case email (legacy / SSO-provisioned) still
    resolves regardless of the case typed into the form — same guarantee
    every other auth path provides.

    Pinned via the rendered WHERE clause; a regression to a bare
    ``User.email == ...`` drops the ``lower(`` marker and trips here.
    """
    _patch_common(monkeypatch)

    # User query returns None → consent error; the focus is the WHERE shape.
    db = _StubDB([_make_client(), None])
    resp = await _call(db, email="Alice@Example.COM", password="whatever1")

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    assert any("lower(" in w.lower() for w in db.where_texts), (
        "authorize_post must use func.lower(User.email); captured WHERE "
        "clauses: " + repr(db.where_texts)
    )


# ── local_auth_enabled gate ─────────────────────────────────────────────


async def test_authorize_post_refuses_when_local_auth_disabled(monkeypatch):
    """On an SSO-only platform (``local_auth_enabled`` False) the endpoint
    must refuse the local password — mirroring the 403 gate in
    ``auth.login`` — and must NOT probe the user table or run
    ``verify_password``.
    """
    _patch_common(monkeypatch, local_auth_enabled=False)

    # Only the client lookup should run; a user query would mean the gate
    # was bypassed.
    db = _StubDB([_make_client()])
    resp = await _call(db, email="alice@example.com", password="Sup3rSecret1")

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    assert "Local authentication is disabled" in resp.body.decode()
    assert db.execute_calls == 1, (
        "with local auth disabled the route must stop after the client "
        "lookup — no user query, no password probe"
    )


# ── rate limiting ───────────────────────────────────────────────────────


async def test_authorize_post_rate_limit_runs_before_any_db_work(monkeypatch):
    """The per-IP throttle must run before the route touches the DB, so a
    flood of requests is rejected with 429 without hammering Postgres or
    probing credentials.
    """

    async def _rate_limited(*_a, **_kw):
        raise HTTPException(status_code=429, detail="Too many requests")

    monkeypatch.setattr(mcp_oauth_module, "enforce_rate_limit", _rate_limited)

    db = _StubDB([_make_client()])  # must never be touched
    with pytest.raises(HTTPException) as exc:
        await _call(db, email="alice@example.com", password="Sup3rSecret1")

    assert exc.value.status_code == 429
    assert db.execute_calls == 0, "rate limit must short-circuit before any DB query"


async def test_authorize_post_throttles_with_login_envelope(monkeypatch):
    """Pin the throttle envelope (bucket name + limit + window) so it stays
    aligned with ``auth.login`` and a future tweak is a conscious change.
    """
    _patch_common(monkeypatch)

    captured: dict[str, object] = {}

    async def _rate_limit(_request, name, *, limit, window_seconds):
        captured.update(name=name, limit=limit, window_seconds=window_seconds)

    monkeypatch.setattr(mcp_oauth_module, "enforce_rate_limit", _rate_limit)

    db = _StubDB([_make_client(), None])
    await _call(db, email="alice@example.com", password="whatever1")

    assert captured["name"] == "mcp-oauth-authorize"
    assert captured["limit"] == 10
    assert captured["window_seconds"] == 60


# ── two-factor ──────────────────────────────────────────────────


def _capture_stored_codes(monkeypatch) -> list[dict]:
    """Record every authorization code the route stores (none = no grant)."""
    stored: list[dict] = []

    async def _store(code, **kw):
        stored.append({"code": code, **kw})

    monkeypatch.setattr(mcp_oauth_module, "_store_auth_code", _store)
    return stored


def _mfa_user(password: str, *, mfa_enabled: bool = True):
    """A user carrying every attribute the password and code steps read."""
    secret = mfa.generate_secret()
    user = SimpleNamespace(
        id=uuid.uuid4(),
        email="alice@example.com",
        full_name="Alice Example",
        is_active=True,
        hashed_password=security.hash_password(password),
        password_changed_at=None,
        mfa_enabled=mfa_enabled,
        mfa_secret=mfa.encrypt_secret(secret) if mfa_enabled else None,
        mfa_confirmed_at=None,
        mfa_last_used_step=None,
        mfa_recovery_codes=None,
    )
    return user, secret


def _code_for(secret: str, step: int) -> str:
    return pyotp.TOTP(
        secret, digits=mfa.TOTP_DIGITS, interval=mfa.TOTP_PERIOD_SECONDS
    ).generate_otp(step)


def _a_wrong_code(secret: str) -> str:
    """A well-formed code that no step near now produces (never a literal,
    which is the right code one time in a million)."""
    step = mfa.current_step(time.time())
    reach = mfa.TOTP_DRIFT_STEPS + 1
    accepted = {_code_for(secret, s) for s in range(step - reach, step + reach + 1)}
    candidate = int(_code_for(secret, step))
    while (code := f"{candidate:0{mfa.TOTP_DIGITS}d}") in accepted:
        candidate = (candidate + 1) % 10**mfa.TOTP_DIGITS
    return code


def _challenge_in(html: str) -> str:
    match = re.search(r'name="mfa_token" value="([^"]+)"', html)
    assert match, "the code step must carry the challenge back"
    return match.group(1)


async def _password_step(user, password):
    """Run the password step for *user*; return its page and the challenge."""
    db = _StubDB([_make_client(), user], user=user)
    resp = await _call(db, email=user.email, password=password)
    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    # The page carries the challenge: no browser or proxy may keep it.
    assert resp.headers["cache-control"] == "no-store"
    html = resp.body.decode()
    return html, _challenge_in(html)


async def test_mfa_user_gets_no_code_for_the_right_password_alone(monkeypatch):
    """Email + password must not mint a code for an
    account with two-factor enabled. The answer is the code step, carrying
    the same scoped challenge ``POST /auth/login`` hands out."""
    audit_calls = _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, _secret = _mfa_user(password)

    html, challenge = await _password_step(user, password)

    assert stored == [], "no authorization code may exist before the second factor"
    assert "code=" not in html
    assert 'name="code"' in html
    assert 'name="password"' not in html
    # A challenge, not a session: it decodes as the MFA scope for this user,
    # and every session surface refuses it.
    assert mfa.decode_challenge_token(challenge).user_id == user.id
    assert security.is_confined_token(security.decode_access_token(challenge))
    # The pending factor is on record, as on ``/auth/login``.
    assert [c["details"] for c in audit_calls] == [
        {"login_method": "mcp_oauth", "stage": "password"}
    ]


async def test_mfa_user_gets_a_code_with_a_valid_totp(monkeypatch):
    audit_calls = _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, secret = _mfa_user(password)
    _html, challenge = await _password_step(user, password)

    step = mfa.current_step()
    db = _StubDB([_make_client()], user=user)
    resp = await _call(db, mfa_token=challenge, code=_code_for(secret, step))

    # A grant answers with the page that returns to the client.
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    location, _ = _return_page(resp)
    assert location.startswith(REDIRECT_URI)
    assert "state=xyz" in location
    assert len(stored) == 1
    assert f"code={stored[0]['code']}" in location
    assert stored[0]["user_id"] == str(user.id)
    # The spent step is recorded (replay guard) and committed before the code leaves.
    assert user.mfa_last_used_step == step
    assert db.commits >= 1
    grant = audit_calls[-1]["details"]
    assert grant["login_method"] == "mcp_oauth"
    assert grant["mfa"] is True


async def test_mfa_user_gets_no_code_with_a_wrong_totp(monkeypatch):
    audit_calls = _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, secret = _mfa_user(password)
    _html, challenge = await _password_step(user, password)

    db = _StubDB([_make_client()], user=user)
    resp = await _call(db, mfa_token=challenge, code=_a_wrong_code(secret))

    assert isinstance(resp, HTMLResponse)
    assert resp.status_code == 200
    html = resp.body.decode()
    assert stored == []
    assert "Invalid verification code" in html
    # Still the code step, with the same challenge, so the user can retry.
    assert _challenge_in(html) == challenge
    assert user.mfa_last_used_step is None
    # The failure is audited by the shared check, attributed to this surface.
    assert audit_calls[-1]["details"] == {"context": "mcp-oauth", "outcome": "invalid_code"}


async def test_mfa_user_gets_no_code_without_a_totp(monkeypatch):
    """Submitting the code step empty is a wrong code, never a grant."""
    _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, _secret = _mfa_user(password)
    _html, challenge = await _password_step(user, password)

    db = _StubDB([_make_client()], user=user)
    resp = await _call(db, mfa_token=challenge, code="")

    assert isinstance(resp, HTMLResponse)
    assert stored == []
    assert "Invalid verification code" in resp.body.decode()


async def test_mfa_user_gets_a_code_with_a_recovery_code_spent_once(monkeypatch):
    """The other branch of the shared check: a recovery code signs in once,
    in place of the app, and is burned."""
    _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, _secret = _mfa_user(password)
    plaintext, user.mfa_recovery_codes = mfa.generate_recovery_codes()

    _html, challenge = await _password_step(user, password)
    resp = await _call(
        _StubDB([_make_client()], user=user), mfa_token=challenge, code=plaintext[0]
    )
    location, _ = _return_page(resp)
    assert len(stored) == 1
    assert f"code={stored[0]['code']}" in location
    assert mfa.count_unused_recovery_codes(user.mfa_recovery_codes) == (
        mfa.RECOVERY_CODE_COUNT - 1
    )

    _html, challenge = await _password_step(user, password)
    replayed = await _call(
        _StubDB([_make_client()], user=user), mfa_token=challenge, code=plaintext[0]
    )
    assert isinstance(replayed, HTMLResponse)
    assert "Invalid verification code" in replayed.body.decode()
    assert len(stored) == 1


async def test_the_code_step_spends_the_shared_mfa_verify_budget(monkeypatch):
    """No extra guessing budget: each code costs the ``mfa-verify`` bucket
    ``/auth/mfa/verify`` spends, and its 429 is raised as it is, like this
    route's own rate limit."""
    _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, secret = _mfa_user(password)
    _html, challenge = await _password_step(user, password)

    buckets: list[tuple] = []

    async def _spent(_request, name, *, limit, window_seconds, **_kw):
        buckets.append((name, limit, window_seconds))
        raise HTTPException(status_code=429, detail="Too many requests")

    monkeypatch.setattr(auth_api, "enforce_rate_limit", _spent)
    with pytest.raises(HTTPException) as exc:
        await _call(
            _StubDB([_make_client()], user=user),
            mfa_token=challenge,
            code=_code_for(secret, mfa.current_step()),
        )

    assert exc.value.status_code == 429
    assert buckets == [("mfa-verify", 10, 60)]
    assert stored == []
    assert user.mfa_last_used_step is None


async def test_mfa_turned_off_mid_flight_grants_without_claiming_mfa(monkeypatch):
    """Two-factor reset while the challenge was in flight: the password was
    proven and no factor is left, as on ``/auth/mfa/verify``, and the grant's
    audit does not claim a second factor that was never checked."""
    audit_calls = _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, _secret = _mfa_user(password)
    _html, challenge = await _password_step(user, password)

    user.mfa_enabled = False
    user.mfa_secret = None
    resp = await _call(_StubDB([_make_client()], user=user), mfa_token=challenge, code="")

    location, _ = _return_page(resp)
    assert len(stored) == 1
    assert f"code={stored[0]['code']}" in location
    assert "mfa" not in audit_calls[-1]["details"]


async def test_an_account_disabled_mid_flight_gets_no_code(monkeypatch):
    """A valid challenge held by an account disabled since the password step."""
    _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, secret = _mfa_user(password)
    _html, challenge = await _password_step(user, password)

    user.is_active = False
    db = _StubDB([_make_client()], user=user)
    resp = await _call(db, mfa_token=challenge, code=_code_for(secret, mfa.current_step()))

    assert isinstance(resp, HTMLResponse)
    html = resp.body.decode()
    assert "verification session has expired" in html
    assert 'name="password"' in html
    assert stored == []
    # Committed before answering, which also releases the row lock.
    assert db.commits >= 1


async def test_a_bad_challenge_never_falls_back_to_the_password(monkeypatch):
    """Posting the right password next to a forged challenge (or a session
    token in its place) must not grant: the code step never re-reads the
    password, and the page sends the user back to sign in."""
    from app.core.security import create_access_token

    _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, secret = _mfa_user(password)

    for bad in ("not-a-jwt", create_access_token(str(user.id))):
        db = _StubDB([_make_client(), user], user=user)
        resp = await _call(
            db,
            email=user.email,
            password=password,
            mfa_token=bad,
            code=_code_for(secret, mfa.current_step()),
        )
        assert isinstance(resp, HTMLResponse)
        html = resp.body.decode()
        assert "verification session has expired" in html
        assert 'name="password"' in html
    assert stored == []


async def test_user_without_mfa_still_gets_a_code_for_the_password(monkeypatch):
    """Accounts without two-factor are unchanged: one step, straight to the code."""
    audit_calls = _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, _secret = _mfa_user(password, mfa_enabled=False)

    db = _StubDB([_make_client(), user], user=user)
    resp = await _call(db, email=user.email, password=password)

    location, _ = _return_page(resp)
    assert len(stored) == 1
    assert f"code={stored[0]['code']}" in location
    assert [c["details"] for c in audit_calls] == [
        {"login_method": "mcp_oauth", "client_id": "mcp-abc123", "client_name": "VS Code"}
    ]


def test_the_two_steps_over_http_bind_the_real_form_fields(monkeypatch):
    """The steps above call the route function; this posts the two forms the
    page really submits, so a renamed field cannot pass unnoticed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.database import get_db

    _patch_common(monkeypatch)
    stored = _capture_stored_codes(monkeypatch)
    password = "Sup3rSecret1"
    user, secret = _mfa_user(password)
    oauth = {
        "client_id": "mcp-abc123",
        "redirect_uri": REDIRECT_URI,
        "state": "xyz",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "response_type": "code",
        "resource": "",
    }
    app = FastAPI()
    app.include_router(mcp_oauth_module.router, prefix="/mcp/oauth")
    dbs = iter(
        [_StubDB([_make_client(), user], user=user), _StubDB([_make_client()], user=user)]
    )

    async def _get_db():
        yield next(dbs)

    app.dependency_overrides[get_db] = _get_db
    client = TestClient(app, follow_redirects=False)

    first = client.post(
        "/mcp/oauth/authorize", data={**oauth, "email": user.email, "password": password}
    )
    assert first.status_code == 200
    assert stored == []
    second = client.post(
        "/mcp/oauth/authorize",
        data={
            **oauth,
            "mfa_token": _challenge_in(first.text),
            "code": _code_for(secret, mfa.current_step()),
        },
    )
    assert second.status_code == 200
    assert len(stored) == 1
    location, _ = _return_page(HTMLResponse(second.text))
    assert f"code={stored[0]['code']}" in location


def test_the_code_step_page_runs_only_the_hashed_script():
    """The code step is the same page under the same CSP hash, with no inline
    handler, and its script stays off the session and SSO paths."""
    import app.main  # noqa: F401 - registers every page policy
    from app.core.csp import inline_script_hashes, page_policy_for

    html = mcp_oauth_module._consent_html(
        client_name="VS Code",
        client_id="c",
        redirect_uri=REDIRECT_URI,
        mfa_token="header.payload.signature",
    )
    policy = page_policy_for("/mcp/oauth/authorize")
    assert set(inline_script_hashes(html)) <= set(policy.script_hashes())
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE)
    assert 'autocomplete="one-time-code"' in html
    assert "if (document.getElementById('code')) return;" in html
