"""Saving the agent browser's session into a browser profile.

The ``browser`` tool's ``save_session`` action and
``agent_sessions.save_browser_session``: the access checks (bound and usable,
not read-only, manageable by the run's person, not another profile's session,
a page on one of the profile's hosts), the scoping of the saved state to the
profile's hosts, and the probe that decides whether anything is saved.
Nothing here launches a browser.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.browser_profile import BrowserProfile
from app.services import browser_tool
from app.services.browser_profiles import agent_access, agent_sessions, signin
from app.services.browser_profiles.agent_access import ProfileAccess
from app.services.browser_profiles.agent_sessions import ProfileSessionError, ProfileSessionInfo, SessionMark
from app.services.browser_profiles.browser import SESSION_STORAGE_KEY
from app.services.browser_profiles.signin import ProbeResult
from app.services.browser_tool import CapturedSession
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall, ToolResult

A, B = (str(uuid.uuid4()) for _ in range(2))
USER = str(uuid.uuid4())
ACCESS = ProfileAccess(profile_ids=(A,), user_id=USER, agent_name="Analyst")
SAVED_AT = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def _profile(pid: str = A, **fields) -> BrowserProfile:
    fields.setdefault("start_url", "https://app.example.com/home")
    return BrowserProfile(id=uuid.UUID(pid), name="Licensing", **fields)


def _captured(url: str = "https://app.example.com/home", **fields) -> CapturedSession:
    state = {
        "cookies": [
            {"name": "sid", "value": "1", "domain": "app.example.com", "path": "/"},
            {"name": "parent", "value": "2", "domain": ".example.com", "path": "/"},
            {"name": "esctx", "value": "3", "domain": "login.idp.example.net", "path": "/"},
            {"name": "other", "value": "4", "domain": "news.example.org", "path": "/"},
        ],
        "origins": [
            {"origin": "https://app.example.com", "localStorage": [{"name": "token", "value": "t"}]},
            {"origin": "https://news.example.org", "localStorage": [{"name": "x", "value": "y"}]},
        ],
    }
    fields.setdefault("session_storage", {"msal": "s"})
    return CapturedSession(state=state, url=url, **fields)


@pytest.fixture
def saved(monkeypatch):
    """The writes, the audit and the person's right to manage, recorded instead of reaching the database."""
    calls: dict = {"stored": [], "audits": [], "probed": []}

    async def may_manage(access, profile):
        return calls.get("may_manage", True)

    async def store(profile_id, state, probe, message):
        calls["stored"].append((profile_id, state, probe, message))
        return SAVED_AT

    async def audit(access, profile, *, saved, reason):
        calls["audits"].append(saved)

    async def resolved(profile):
        return agent_sessions.ProfileSnapshot(id=str(profile.id), name=profile.name, start_url=profile.start_url, login_url=None)

    async def probe(snap):
        calls["probed"].append(snap.storage_state)
        return calls.get("probe", ProbeResult(authenticated=True, url=snap.start_url, reason="the application rendered"))

    monkeypatch.setattr(agent_sessions, "_may_manage", may_manage)
    monkeypatch.setattr(agent_sessions, "_store_saved_session", store)
    monkeypatch.setattr(agent_sessions, "_audit_save", audit)
    monkeypatch.setattr(agent_sessions, "resolved_snapshot", resolved)
    monkeypatch.setattr(signin, "probe_profile", probe)
    return calls


# ── What is kept ──────────────────────────────────────────────────────────


def test_the_profile_hosts_are_its_urls_and_the_ones_it_declares():
    profile = _profile(login_url="https://auth.example.com/login", allowed_hosts=["login.idp.example.net"])
    assert agent_sessions.profile_hosts(profile) == frozenset(
        {"app.example.com", "auth.example.com", "login.idp.example.net"}
    )


def test_only_the_cookies_and_origins_of_the_profile_hosts_are_kept():
    state = agent_sessions.scoped_state(_captured(), frozenset({"app.example.com"}), "https://app.example.com/home")
    assert [c["name"] for c in state["cookies"]] == ["sid", "parent"]
    assert [o["origin"] for o in state["origins"]] == ["https://app.example.com"]
    assert state[SESSION_STORAGE_KEY] == {"https://app.example.com": {"msal": "s"}}


def test_a_declared_identity_provider_host_keeps_its_cookies():
    hosts = frozenset({"app.example.com", "login.idp.example.net"})
    state = agent_sessions.scoped_state(_captured(), hosts, "https://app.example.com/home")
    assert "esctx" in [c["name"] for c in state["cookies"]]
    assert "other" not in [c["name"] for c in state["cookies"]]


def test_session_storage_is_kept_only_from_the_start_url_origin():
    captured = _captured(url="https://login.idp.example.net/authorize")
    hosts = frozenset({"app.example.com", "login.idp.example.net"})
    assert SESSION_STORAGE_KEY not in agent_sessions.scoped_state(captured, hosts, "https://app.example.com/home")


# ── The save ──────────────────────────────────────────────────────────────


async def test_a_signed_in_session_is_saved_scoped_and_checked(saved):
    text = await agent_sessions.save_browser_session(_profile(), _captured(), access=ACCESS)
    assert "Saved the browser's session into the browser profile 'Licensing'" in text
    assert "2 cookie(s), 1 origin(s)" in text
    (profile_id, state, probe, message), = saved["stored"]
    assert profile_id == A and probe.authenticated
    assert {c["domain"] for c in state["cookies"]} == {"app.example.com", ".example.com"}
    assert saved["probed"] == [state]  # the probe ran on exactly what is saved
    assert "agent Analyst" in message
    assert saved["audits"] == [True]


async def test_a_session_that_does_not_open_the_application_saves_nothing(saved):
    saved["probe"] = ProbeResult(authenticated=False, url="https://login.idp.example.net/", reason="a sign-in form")
    with pytest.raises(ProfileSessionError, match="Nothing was saved"):
        await agent_sessions.save_browser_session(_profile(), _captured(), access=ACCESS)
    assert saved["stored"] == []
    assert saved["audits"] == [False]


async def test_an_application_that_does_not_answer_saves_nothing(saved):
    saved["probe"] = ProbeResult(authenticated=False, url="", reason="HTTP 503", http_status=503)
    with pytest.raises(ProfileSessionError, match="could not be checked"):
        await agent_sessions.save_browser_session(_profile(), _captured(), access=ACCESS)
    assert saved["stored"] == []


async def test_no_open_session_has_nothing_to_save(saved):
    with pytest.raises(ProfileSessionError, match="No browser session is open"):
        await agent_sessions.save_browser_session(_profile(), None, access=ACCESS)


async def test_a_read_only_binding_refuses_the_save(saved):
    access = ProfileAccess(profile_ids=(A,), read_only_ids=frozenset({A}), user_id=USER)
    with pytest.raises(ProfileSessionError, match="read-only"):
        await agent_sessions.save_browser_session(_profile(), _captured(), access=access)
    assert saved["probed"] == [] and saved["stored"] == []


async def test_a_person_who_may_only_use_the_profile_cannot_save_into_it(saved):
    saved["may_manage"] = False
    with pytest.raises(ProfileSessionError, match="not edit it"):
        await agent_sessions.save_browser_session(_profile(), _captured(), access=ACCESS)
    assert saved["probed"] == [] and saved["stored"] == []


async def test_another_profiles_session_is_never_saved_into_this_one(saved):
    other = ProfileSessionInfo(profile_id=B, name="Staging", start_url="https://app.example.com/", read_only=False)
    with pytest.raises(ProfileSessionError, match="signed in with the browser profile 'Staging'"):
        await agent_sessions.save_browser_session(_profile(), _captured(profile=other), access=ACCESS)
    assert saved["stored"] == []


async def test_the_session_of_this_profile_may_be_saved_again(saved):
    mine = ProfileSessionInfo(profile_id=A, name="Licensing", start_url="https://app.example.com/home", read_only=False)
    await agent_sessions.save_browser_session(_profile(), _captured(profile=mine), access=ACCESS)
    assert len(saved["stored"]) == 1


async def test_a_save_moves_the_write_back_mark_of_the_session_it_came_from(saved):
    """The release of a session saved into its own profile compares against that save, not the one at open."""
    mark = SessionMark(datetime(2026, 11, 1, tzinfo=UTC))
    mine = ProfileSessionInfo(
        profile_id=A, name="Licensing", start_url="https://app.example.com/home", read_only=False, mark=mark
    )
    await agent_sessions.save_browser_session(_profile(), _captured(profile=mine), access=ACCESS)
    assert mark.saved_at == SAVED_AT


async def test_a_refused_save_leaves_the_write_back_mark(saved):
    opened = datetime(2026, 11, 1, tzinfo=UTC)
    mark = SessionMark(opened)
    mine = ProfileSessionInfo(
        profile_id=A, name="Licensing", start_url="https://app.example.com/home", read_only=False, mark=mark
    )
    saved["probe"] = ProbeResult(authenticated=False, url="https://login.idp.example.net/", reason="a sign-in form")
    with pytest.raises(ProfileSessionError):
        await agent_sessions.save_browser_session(_profile(), _captured(profile=mine), access=ACCESS)
    assert mark.saved_at == opened


async def test_the_write_back_after_a_save_compares_against_the_save(monkeypatch):
    """``release()`` passes the mark's current instant to ``store_refreshed_session``."""
    from contextlib import asynccontextmanager

    from app.services.browser_profiles import browser as profile_browser_mod

    opened = datetime(2026, 11, 1, tzinfo=UTC)
    seen: list = []
    rb = SimpleNamespace(pw=None, browser=None, context=None, page=None, redirects=[])

    @asynccontextmanager
    async def fake_browser(**_kwargs):
        yield rb

    async def resolved(profile):
        return agent_sessions.ProfileSnapshot(
            id=str(profile.id), name=profile.name, start_url=profile.start_url, login_url=None
        )

    async def write_back(_rb, _snap, saved_at):
        seen.append(saved_at)

    async def audit_use(*_a, **_k):
        return None

    monkeypatch.setattr(profile_browser_mod, "profile_browser", fake_browser)
    monkeypatch.setattr(agent_sessions, "resolved_snapshot", resolved)
    monkeypatch.setattr(agent_sessions, "_write_back", write_back)
    monkeypatch.setattr(agent_sessions, "_audit_use", audit_use)
    row = _profile(session_saved_at=opened)
    session = await agent_sessions.open_profile_session(row, access=ACCESS)
    session.profile.mark.saved_at = SAVED_AT  # what save_browser_session does after a save
    await session.release()
    assert seen == [SAVED_AT]


async def test_a_page_off_the_profile_hosts_is_refused(saved):
    with pytest.raises(ProfileSessionError, match="not a host of the browser profile"):
        await agent_sessions.save_browser_session(_profile(), _captured(url="https://news.example.org/"), access=ACCESS)
    assert saved["probed"] == []


async def test_a_session_with_nothing_of_the_profile_hosts_is_refused(saved):
    captured = CapturedSession(state={"cookies": [], "origins": []}, url="https://app.example.com/", session_storage={})
    with pytest.raises(ProfileSessionError, match="nothing to save"):
        await agent_sessions.save_browser_session(_profile(), captured, access=ACCESS)
    assert saved["probed"] == []


# ── The tool's action ─────────────────────────────────────────────────────


async def _call(arguments: dict, access=ACCESS) -> ToolResult:
    _defs, executor = build_browser_tool("agent:conv", profile_access=access)
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


def test_save_session_is_offered_only_to_a_run_with_bindings():
    plain, _ = build_browser_tool("agent:conv")
    assert "save_session" not in plain[0].input_schema["properties"]["action"]["enum"]
    bound, _ = build_browser_tool("agent:conv", profile_access=ACCESS)
    assert "save_session" in bound[0].input_schema["properties"]["action"]["enum"]
    assert "save_session:" in bound[0].description


async def test_the_action_saves_the_open_session_into_the_named_profile(monkeypatch):
    row = _profile()
    seen = []

    async def resolve(access, reference):
        seen.append(reference)
        return row

    async def capture(key):
        seen.append(key)
        return _captured()

    async def save(profile, captured, *, access):
        seen.append((profile.id, captured.url, access.user_id))
        return "Saved."

    monkeypatch.setattr(agent_access, "resolve_profile", resolve)
    monkeypatch.setattr(browser_tool, "capture_session", capture)
    monkeypatch.setattr(agent_sessions, "save_browser_session", save)
    result = await _call({"action": "save_session", "profile": "Licensing"})
    assert not result.is_error
    assert result.content == "Saved."
    assert seen == ["Licensing", "agent:conv", (row.id, "https://app.example.com/home", USER)]


async def test_the_action_needs_a_profile():
    result = await _call({"action": "save_session"})
    assert result.is_error
    assert "'profile' is required" in result.content


async def test_a_refused_save_is_an_error_with_the_reason(monkeypatch):
    async def resolve(access, reference):
        return _profile()

    async def capture(key):
        return None

    monkeypatch.setattr(agent_access, "resolve_profile", resolve)
    monkeypatch.setattr(browser_tool, "capture_session", capture)
    result = await _call({"action": "save_session", "profile": "Licensing"})
    assert result.is_error
    assert "No browser session is open" in result.content


async def test_without_bindings_save_session_is_an_unknown_action():
    result = await _call({"action": "save_session"}, access=None)
    assert result.is_error
    assert "Unknown action" in result.content
    assert "profiles, save_session" not in result.content


# ── Reading the open session ──────────────────────────────────────────────


async def test_capture_never_opens_a_session():
    assert await browser_tool.capture_session(f"agent:{uuid.uuid4()}") is None


async def test_capture_reads_the_context_state_the_url_and_the_session_storage():
    async def storage_state():
        return {"cookies": [{"name": "sid", "domain": "app.example.com"}], "origins": []}

    async def evaluate(_script):
        return {"msal": "s"}

    page = SimpleNamespace(on=lambda *_a, **_k: None, url="https://app.example.com/home", evaluate=evaluate,
                           is_closed=lambda: False)
    session = browser_tool.BrowserSession(
        pw=None, browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(storage_state=storage_state),
        page=page,
    )
    key = f"agent:{uuid.uuid4()}"
    browser_tool._sessions[key] = session
    try:
        captured = await browser_tool.capture_session(key)
    finally:
        browser_tool._sessions.pop(key, None)
    assert captured is not None
    assert captured.url == "https://app.example.com/home"
    assert captured.state["cookies"][0]["name"] == "sid"
    assert captured.session_storage == {"msal": "s"}
    assert captured.profile is None


async def test_capture_reads_indexed_db_when_playwright_can():
    calls: list = []

    async def storage_state(**kwargs):
        calls.append(kwargs)
        return {"cookies": [], "origins": [{"origin": "https://app.example.com", "indexedDB": [{"name": "msal"}]}]}

    async def evaluate(_script):
        return {}

    page = SimpleNamespace(on=lambda *_a, **_k: None, url="https://app.example.com/home", evaluate=evaluate,
                           is_closed=lambda: False)
    session = browser_tool.BrowserSession(
        pw=None, browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(storage_state=storage_state),
        page=page,
    )
    key = f"agent:{uuid.uuid4()}"
    browser_tool._sessions[key] = session
    try:
        captured = await browser_tool.capture_session(key)
    finally:
        browser_tool._sessions.pop(key, None)
    assert calls == [{"indexed_db": True}]
    assert captured is not None and captured.state["origins"][0]["indexedDB"] == [{"name": "msal"}]
