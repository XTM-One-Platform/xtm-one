"""Browser profiles every agent can browse with.

The rules of ``services/browser_profiles/agent_access.py`` (a profile opens
only when it is bound AND usable by the person the run acts for), the
``browser`` tool's profile surface (the ``profiles`` action, ``navigate`` with
``profile``, read-only bindings), the browser session hooks it relies on, and
the compare-and-set that keeps a newer saved session from being overwritten.
Nothing here launches a browser; the sign-in itself is
``test_browser_profile_signin*.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.browser_profile import BrowserProfile
from app.services import browser_tool
from app.services.browser_profiles import agent_access, agent_sessions
from app.services.browser_profiles.agent_access import (
    MAX_BOUND_PROFILES,
    ProfileAccess,
    ProfileReferenceError,
    access_for_agent,
    access_for_general_assistant,
    normalize_profile_ids,
    normalize_read_only,
    resolve_profile,
)
from app.services.browser_profiles.agent_sessions import ProfileSessionError, ProfileSessionInfo
from app.services.builtin_tools import build_browser_tool
from app.tools.base import ToolCall, ToolResult

A, B, C = (str(uuid.uuid4()) for _ in range(3))
USER = str(uuid.uuid4())


def _profile(pid: str, name: str, **fields) -> BrowserProfile:
    return BrowserProfile(id=uuid.UUID(pid), name=name, start_url=f"https://{name.lower()}.example.com/", **fields)


# ── Stored bindings ───────────────────────────────────────────────────────


def test_bound_ids_are_canonical_deduplicated_and_ordered():
    assert normalize_profile_ids([A.upper(), B, A]) == [A, B]
    assert normalize_profile_ids([]) is None
    assert normalize_profile_ids(None) is None


@pytest.mark.parametrize("value", [["not-a-uuid"], [None], "a-string"])
def test_a_bound_id_that_is_not_a_profile_id_is_refused(value):
    with pytest.raises(ValueError):
        normalize_profile_ids(value)


def test_an_agent_binds_a_bounded_number_of_profiles():
    with pytest.raises(ValueError, match=str(MAX_BOUND_PROFILES)):
        normalize_profile_ids([str(uuid.uuid4()) for _ in range(MAX_BOUND_PROFILES + 1)])


def test_the_read_only_list_keeps_only_bound_profiles():
    assert normalize_read_only([B, C], [A, B]) == [B]
    assert normalize_read_only([C], [A, B]) is None
    assert normalize_read_only(None, [A]) is None


# ── Who may open what ─────────────────────────────────────────────────────


def test_an_agent_without_bindings_opens_no_profile():
    assert access_for_agent(SimpleNamespace(id=uuid.uuid4(), name="x", browser_profile_ids=None), user_id=USER) is None


def test_a_run_reads_its_agent_bindings_and_skips_what_is_not_an_id():
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Analyst", browser_profile_ids=[A, "garbage", B], browser_profile_read_only=[B, C]
    )
    access = access_for_agent(agent, user_id=USER)
    assert access is not None
    assert access.profile_ids == (A, B)
    assert access.read_only_ids == frozenset({B})
    assert access.user_id == USER
    assert access.is_read_only(B) and not access.is_read_only(A)


def test_a_run_that_acts_for_nobody_carries_no_person():
    agent = SimpleNamespace(id=uuid.uuid4(), name="x", browser_profile_ids=[A], browser_profile_read_only=None)
    assert access_for_agent(agent, user_id=None).user_id is None


def test_the_general_assistant_opens_what_the_person_gave_it():
    access = access_for_general_assistant([A], user_id=USER)
    assert access.profile_ids == (A,)
    assert access.via == agent_access.VIA_GENERAL_ASSISTANT
    assert access_for_general_assistant(None, user_id=USER) is None


async def test_no_binding_or_no_person_resolves_no_profile():
    with pytest.raises(ProfileReferenceError, match="bound to no browser profile"):
        await resolve_profile(None, "Demo")
    with pytest.raises(ProfileReferenceError, match="acts for nobody"):
        await resolve_profile(ProfileAccess(profile_ids=(A,)), "Demo")


async def test_a_profile_resolves_by_id_or_exact_name_among_the_usable_bound_ones(monkeypatch):
    rows = [_profile(A, "Demo"), _profile(B, "Staging")]

    async def usable(access):
        return rows

    monkeypatch.setattr(agent_access, "usable_profiles", usable)
    access = ProfileAccess(profile_ids=(A, B), user_id=USER)
    assert (await resolve_profile(access, B)).name == "Staging"
    assert (await resolve_profile(access, " demo ")).id == uuid.UUID(A)
    with pytest.raises(ProfileReferenceError, match="No browser profile 'Prod'"):
        await resolve_profile(access, "Prod")


async def test_two_profiles_of_one_name_ask_for_the_id(monkeypatch):
    async def usable(access):
        return [_profile(A, "Demo"), _profile(B, "Demo")]

    monkeypatch.setattr(agent_access, "usable_profiles", usable)
    with pytest.raises(ProfileReferenceError, match="give its id"):
        await resolve_profile(ProfileAccess(profile_ids=(A, B), user_id=USER), "Demo")


async def test_bound_profiles_the_person_may_not_use_resolve_nothing(monkeypatch):
    async def usable(access):
        return []

    monkeypatch.setattr(agent_access, "usable_profiles", usable)
    with pytest.raises(ProfileReferenceError, match="may use"):
        await resolve_profile(ProfileAccess(profile_ids=(A,), user_id=USER), A)


# ── The tool's surface ────────────────────────────────────────────────────


def _schema(access):
    defs, _executor = build_browser_tool("agent:conv", profile_access=access)
    return defs[0]


def test_the_profile_surface_is_offered_only_to_a_run_with_bindings():
    plain = _schema(None)
    assert "profile" not in plain.input_schema["properties"]
    assert "profiles" not in plain.input_schema["properties"]["action"]["enum"]
    bound = _schema(ProfileAccess(profile_ids=(A,), user_id=USER))
    assert "profile" in bound.input_schema["properties"]
    assert "profiles" in bound.input_schema["properties"]["action"]["enum"]
    assert "Signed-in browsing" in bound.description


async def _call(arguments: dict, access=None, key: str = "agent:conv") -> ToolResult:
    _defs, executor = build_browser_tool(key, profile_access=access)
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


async def test_a_profile_without_bindings_is_refused_with_the_reason():
    result = await _call({"action": "navigate", "url": "https://example.com", "profile": "Demo"})
    assert result.is_error
    assert "bound to no browser profile" in result.content


async def test_profile_is_a_navigate_argument_only():
    result = await _call({"action": "click", "selector": "a", "profile": "Demo"})
    assert result.is_error
    assert "navigate action only" in result.content


async def test_the_profiles_action_lists_the_usable_bound_profiles(monkeypatch):
    rows = [_profile(A, "Demo", session_status="none"), _profile(B, "Staging", session_status="none")]

    async def usable(access):
        return rows

    monkeypatch.setattr(agent_access, "usable_profiles", usable)
    access = ProfileAccess(profile_ids=(A, B, C), read_only_ids=frozenset({B}), user_id=USER)
    result = await _call({"action": "profiles"}, access)
    assert not result.is_error
    assert "**Demo**" in result.content and "full access" in result.content
    assert "**Staging**" in result.content and "read-only" in result.content
    assert "1 more bound profile" in result.content


@pytest.mark.parametrize(
    ("action", "extra"),
    [("click", {"selector": "a"}), ("type", {"selector": "input", "text": "x"}), ("press", {"key": "Enter"}),
     ("hover", {"selector": "a"}), ("evaluate", {"expression": "1"})],
)
async def test_a_read_only_profile_session_refuses_what_acts_on_the_page(monkeypatch, action, extra):
    info = ProfileSessionInfo(profile_id=A, name="Demo", start_url="https://demo.example.com/", read_only=True)
    monkeypatch.setattr(browser_tool, "session_profile", lambda key: info)

    async def never(*_args, **_kwargs):
        raise AssertionError("the action reached the page")

    for name in ("browser_click", "browser_type", "browser_press", "browser_hover", "browser_evaluate"):
        monkeypatch.setattr(browser_tool, name, never)
    result = await _call({"action": action, **extra}, ProfileAccess(profile_ids=(A,), user_id=USER))
    assert result.is_error
    assert "read-only" in result.content


async def test_navigate_with_a_profile_opens_it_and_lands_on_its_start_url(monkeypatch):
    row = _profile(A, "Demo")
    opened = []
    info = ProfileSessionInfo(profile_id=A, name="Demo", start_url=row.start_url, read_only=False)

    async def resolve(access, reference):
        return row

    async def open_session(profile, *, access):
        opened.append(profile.id)
        return SimpleNamespace(profile=info)

    async def install(key, session):
        opened.append(key)

    async def navigate(key, url):
        return f"Navigated to: {url}\nTitle: Demo"

    monkeypatch.setattr(agent_access, "resolve_profile", resolve)
    monkeypatch.setattr(agent_sessions, "open_profile_session", open_session)
    monkeypatch.setattr(browser_tool, "install_session", install)
    monkeypatch.setattr(browser_tool, "session_profile", lambda key: None)
    monkeypatch.setattr(browser_tool, "browser_navigate", navigate)
    result = await _call({"action": "navigate", "profile": "Demo"}, ProfileAccess(profile_ids=(A,), user_id=USER))
    assert not result.is_error
    assert opened == [row.id, "agent:conv"]
    assert "Signed in with the browser profile 'Demo' (full access)" in result.content
    assert row.start_url in result.content


async def test_a_profile_that_cannot_open_signed_in_says_what_to_ask(monkeypatch):
    async def resolve(access, reference):
        return _profile(A, "Demo")

    async def open_session(profile, *, access):
        raise ProfileSessionError("The browser profile 'Demo' is not signed in. Ask the person to sign it in.")

    monkeypatch.setattr(agent_access, "resolve_profile", resolve)
    monkeypatch.setattr(agent_sessions, "open_profile_session", open_session)
    monkeypatch.setattr(browser_tool, "session_profile", lambda key: None)
    result = await _call({"action": "navigate", "profile": "Demo"}, ProfileAccess(profile_ids=(A,), user_id=USER))
    assert result.is_error
    assert "Ask the person to sign it in" in result.content


async def test_a_profile_with_no_session_and_no_way_to_sign_in_is_refused_before_any_browser(monkeypatch):
    profile = _profile(A, "Demo", login_url="https://demo.example.com/login")

    def no_browser(**_kwargs):
        raise AssertionError("a browser was launched")

    monkeypatch.setattr("app.services.browser_profiles.browser.profile_browser", no_browser)
    with pytest.raises(ProfileSessionError, match="cannot sign in by itself"):
        await agent_sessions.open_profile_session(profile, access=ProfileAccess(profile_ids=(A,), user_id=USER))


# ── The session hooks of browser_tool ─────────────────────────────────────


def _session(**fields) -> browser_tool.BrowserSession:
    page = SimpleNamespace(on=lambda *_a, **_k: None, url="about:blank")
    return browser_tool.BrowserSession(pw=None, browser=None, context=None, page=page, **fields)


async def test_an_installed_session_replaces_and_releases_the_previous_one(monkeypatch):
    released = []

    async def release_first():
        released.append("first")

    async def no_cleanup():
        return None

    monkeypatch.setattr(browser_tool, "_ensure_cleanup_running", no_cleanup)
    key = f"agent:{uuid.uuid4()}"
    first = _session(release=release_first)
    second = _session(release=None)
    try:
        await browser_tool.install_session(key, first)
        await browser_tool.install_session(key, second)
        assert released == ["first"]
        assert browser_tool._sessions[key] is second
    finally:
        browser_tool._sessions.pop(key, None)


async def test_a_session_checks_urls_with_its_own_policy(monkeypatch):
    async def profile_policy(url):
        raise ValueError("not a host the profile declares")

    async def outbound(url):
        return url

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", outbound)
    # Without a profile: the outbound policy (plus a started sandbox service's preview).
    assert browser_tool._url_check(None) is browser_tool._outbound_or_preview
    assert await browser_tool._url_check(None)("https://x.example") == "https://x.example"
    assert await browser_tool._url_policy_refusal("https://x.example", _session(check_url=profile_policy)) == (
        "not a host the profile declares"
    )
    assert await browser_tool._url_policy_refusal("https://x.example", _session()) is None


# ── The write-back never overwrites a newer session ───────────────────────


def test_the_saved_session_is_compared_by_instant():
    now = datetime.now(UTC)
    assert agent_sessions._same_instant(now, now.replace(tzinfo=None).replace(tzinfo=UTC))
    assert agent_sessions._same_instant(None, None)
    assert not agent_sessions._same_instant(now, now + timedelta(seconds=1))
    assert not agent_sessions._same_instant(None, now)
