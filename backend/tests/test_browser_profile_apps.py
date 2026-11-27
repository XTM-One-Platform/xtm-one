"""A browser profile declares the other applications its saved session signs in, and agents can delete one.

An application that signs in through another one's identity provider opens signed in with that
application's session: the profile names it in ``apps`` (``{name, url}``). Its host is then a host of the
profile (the browser policy, the hosts a session is saved for, a lookup by URL), a recording or page
snapshot on it is judged as that application, and the session check reports each one. A profile without
``apps`` reads exactly as before.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from app.api.v1.schemas.browser_profiles import BrowserProfileWrite
from app.models.browser_profile import BrowserProfile
from app.services.browser_profiles import profiles
from app.services.browser_profiles.agent_sessions import profile_hosts


def _profile(**overrides: Any) -> BrowserProfile:
    values = {"name": "Portal", "start_url": "https://portal.example.com/home", "login_url": None}
    values.update(overrides)
    return BrowserProfile(**values)


def test_apps_are_cleaned_one_per_host_with_a_name() -> None:
    apps = profiles.clean_apps(
        [
            {"name": " Risk ", "url": "https://risk.example.com/"},
            {"name": "Risk again", "url": "https://RISK.example.com/other"},
            {"url": "https://reports.example.com/"},
        ]
    )
    assert apps == [
        {"name": "Risk", "url": "https://risk.example.com/"},
        {"name": "reports.example.com", "url": "https://reports.example.com/"},
    ]
    assert profiles.clean_apps(None) == []
    assert profiles.clean_apps([]) == []


@pytest.mark.parametrize(
    "value",
    [
        "https://risk.example.com/",
        [{"name": "Risk"}],
        [{"name": "Risk", "url": "ftp://risk.example.com/"}],
        [{"name": "Risk", "url": "https://user:secret@risk.example.com/"}],
        [{"name": "Risk", "url": "https://risk.example.com/", "password": "x"}],
        [{"url": f"https://app{i}.example.com/"} for i in range(profiles.MAX_APPS + 1)],
    ],
)
def test_a_malformed_app_list_is_refused(value: Any) -> None:
    with pytest.raises(profiles.ProfileError):
        profiles.clean_apps(value)


def test_apps_are_written_served_and_hosts_of_the_profile() -> None:
    profile = _profile()
    fields = profiles.clean_fields(
        name="Portal",
        start_url="https://portal.example.com/home",
        apps=[{"name": "Risk", "url": "https://risk.example.com/"}],
    )
    profiles.apply_fields(profile, fields)
    assert profile.apps == [{"name": "Risk", "url": "https://risk.example.com/"}]
    served = profiles.serialize_profile(profile, can_manage=True)
    assert served["apps"] == [{"name": "Risk", "url": "https://risk.example.com/"}]
    assert "risk.example.com" in profile_hosts(profile)
    assert "risk.example.com" in profiles.destination(profile)[2]


def test_a_profile_without_apps_reads_as_before() -> None:
    profile = _profile()
    profiles.apply_fields(profile, profiles.clean_fields(name="Portal", start_url="https://portal.example.com/home"))
    assert profile.apps is None
    assert profiles.serialize_profile(profile, can_manage=True)["apps"] == []
    assert profile_hosts(profile) == frozenset({"portal.example.com"})
    snap = profiles.snapshot(profile)
    assert snap.apps == ()
    assert snap.scoped_to("https://portal.example.com/x") is snap
    assert snap.scoped_to("https://risk.example.com/") is snap


def test_a_lookup_by_url_finds_the_profile_of_a_declared_application() -> None:
    profile = _profile(apps=[{"name": "Risk", "url": "https://risk.example.com/"}])
    assert profiles.covers_url(profile, "https://risk.example.com/dashboard")
    assert profiles.covers_url(profile, "portal.example.com")
    assert profiles.covers_url(profile, "example.com")
    assert not profiles.covers_url(profile, "https://other.example.org/")
    assert not profiles.covers_url(profile, "")


def test_a_declared_application_is_judged_as_itself_in_the_session() -> None:
    profile = _profile(
        login_url="https://portal.example.com/login",
        login_config={"signed_in_selector": "#portal-menu", "username_selector": "#user"},
        apps=[{"name": "Risk", "url": "https://risk.example.com/"}],
    )
    snap = profiles.snapshot(profile)
    scoped = snap.scoped_to("https://risk.example.com/reports")
    assert scoped.start_url == "https://risk.example.com/"
    assert scoped.app_host == "risk.example.com"
    assert scoped.login_url is None
    # The own application's marks do not judge another one; its sign-in settings stay.
    assert scoped.login_config == {"username_selector": "#user"}
    assert scoped.storage_state == snap.storage_state
    assert snap.scoped_to("https://portal.example.com/") is snap


def test_the_write_body_takes_apps_and_refuses_an_unknown_key() -> None:
    body = BrowserProfileWrite.model_validate(
        {
            "name": "Portal",
            "start_url": "https://portal.example.com/",
            "apps": [{"name": "Risk", "url": "https://risk.example.com/"}],
        }
    )
    assert body.apps is not None and body.apps[0].url == "https://risk.example.com/"
    bare = BrowserProfileWrite.model_validate({"name": "Portal", "start_url": "https://portal.example.com/"})
    assert bare.apps is None
    with pytest.raises(ValueError):
        BrowserProfileWrite.model_validate(
            {
                "name": "Portal",
                "start_url": "https://p.example.com/",
                "apps": [{"url": "https://r.example.com", "x": 1}],
            }
        )


def test_the_tools_declare_apps_the_url_lookup_and_the_delete() -> None:
    from app.services.builtin_tools.video_studio import build_video_studio_tools

    definitions, executor = build_video_studio_tools(user_id="")
    by_name = {d.name: d for d in definitions}
    assert by_name["save_browser_profile"].input_schema["properties"]["apps"]["items"]["required"] == ["url"]
    assert "url" in by_name["list_browser_profiles"].input_schema["properties"]
    delete = by_name["delete_browser_profile"]
    assert delete.input_schema["required"] == ["profile_id"]
    assert set(delete.input_schema["properties"]) == {"profile_id", "confirm"}
    assert not delete.live_state

    def params(name: str) -> set[str]:
        handler: Callable[..., Any] | None = executor.handler(name)
        assert handler is not None
        return set(inspect.signature(handler).parameters)

    assert params("delete_browser_profile") == {"profile_id", "confirm"}
    assert "apps" in params("save_browser_profile")
    assert "url" in params("list_browser_profiles")
