# SPDX-License-Identifier: Apache-2.0

"""The sources #2436 adds: named, off by default, one listener row each, recorded only once included."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.builtin_agents import CTEM_OPENAEV_LOOP_EVENTS
from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signal_sources as sources
from app.services.organization_context import SignalSourcesSettings

NEW_SOURCES = {
    ("relevant_threat", "opencti", "indicator_sighting"),
    ("code_flaw", "github", "code_scanning"),
    ("credential_leak", "github", "secret_scanning"),
    ("new_exposure", "openaev", "finding_created"),
    ("surface_drift", "openaev", "asset_created"),
}


def _match(provider: str, event_key: str, payload: Mapping[str, Any]) -> tuple[listeners.Listener, dict[str, Any]]:
    found = listeners.match(provider, event_key, payload)
    assert found is not None
    return found


def _profile(
    excluded: dict[str, list[str]] | None = None, included: dict[str, list[str]] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        signal_sources=SignalSourcesSettings(excluded=excluded or {}, included=included or {}),
    )


def test_every_new_source_is_named_off_by_default_and_has_its_listener_row() -> None:
    declared = {
        (path, s.provider, s.name) for path, row in sources.PATH_SOURCES.items() for s in row if not s.default_on
    }
    assert declared == NEW_SOURCES
    rows = {(r.trigger_path, r.provider, r.source) for r in listeners.LISTENERS if r.source}
    assert rows == NEW_SOURCES


def test_every_named_listener_row_names_a_source_of_its_path() -> None:
    for row in listeners.LISTENERS:
        keys = {s.key for s in sources.PATH_SOURCES[row.trigger_path]}
        assert sources.source_key(row.provider, "listener", row.source) in keys, row


def test_a_default_off_source_is_excluded_until_included() -> None:
    excluded = sources.parse_excluded(_profile())
    assert sources.is_excluded(excluded, "code_flaw", "github", "listener", "code_scanning")
    assert not sources.is_excluded(excluded, "code_flaw", "github", "listener")
    on = sources.parse_excluded(_profile(included={"code_flaw": ["github:listener:code_scanning"]}))
    assert not sources.is_excluded(on, "code_flaw", "github", "listener", "code_scanning")
    # The Dependabot listener of the same provider is switched apart.
    off = sources.parse_excluded(_profile(excluded={"code_flaw": ["github:listener"]}))
    assert sources.is_excluded(off, "code_flaw", "github", "listener")
    assert sources.is_excluded(off, "code_flaw", "github", "listener", "code_scanning")


def test_an_unreadable_profile_keeps_the_default_off_sources_off() -> None:
    excluded = sources.parse_excluded(None)
    assert sources.is_excluded(excluded, "surface_drift", "openaev", "listener", "asset_created")


def test_the_profile_refuses_a_key_that_is_not_a_source_of_the_path() -> None:
    with pytest.raises(ValueError):
        SignalSourcesSettings(included={"code_flaw": ["github:listener:secret_scanning"]})
    ok = SignalSourcesSettings(included={"credential_leak": ["github:listener:secret_scanning"]})
    assert ok.included == {"credential_leak": ["github:listener:secret_scanning"]}


def test_a_named_source_row_carries_its_key_and_default() -> None:
    bound = sources.BoundSources({("github", "listener")})
    bound.excluded = sources.parse_excluded(_profile())
    rows: dict[str, dict[str, Any]] = {r["key"]: r for r in sources.path_sources("code_flaw", bound)}
    assert rows["github:listener"]["bound"] is True and rows["github:listener"]["default_on"] is True
    named = rows["github:listener:code_scanning"]
    assert named["name"] == "code_scanning" and named["default_on"] is False
    assert named["bound"] is False and named["excluded"] is True and named["available"] is True


def test_the_openaev_platform_source_provisions_the_new_events() -> None:
    assert {"finding_created", "asset_created"} <= set(CTEM_OPENAEV_LOOP_EVENTS)


# ── recorded signals ──


def _secret(action: str = "created") -> dict[str, Any]:
    return {
        "action": action,
        "alert": {
            "number": 4,
            "secret_type": "aws_access_key_id",
            "secret_type_display_name": "AWS Access Key ID",
            "secret": "AKIA-never-read",
            "validity": "active",
            "publicly_leaked": False,
            "html_url": "https://github.com/filigran/portal/security/secret-scanning/4",
        },
        "repository": {"id": 991, "full_name": "filigran/portal"},
    }


def _code(action: str = "created") -> dict[str, Any]:
    return {
        "action": action,
        "alert": {
            "number": 7,
            "html_url": "https://github.com/filigran/portal/security/code-scanning/7",
            "rule": {
                "id": "js/sql-injection",
                "description": "Database query built from user input",
                "security_severity_level": "high",
            },
            "tool": {"name": "CodeQL"},
            "most_recent_instance": {"location": {"path": "src/db.js"}},
        },
        "repository": {"id": 991, "full_name": "filigran/portal"},
    }


def test_a_secret_scanning_alert_is_a_path_10_signal_and_never_carries_the_secret() -> None:
    row, fields = _match("github", "secret_scanning_alert.created", _secret())
    assert (row.trigger_path, row.source) == ("credential_leak", "secret_scanning")
    assert fields["external_id"] == "991:secret:4"
    evidence = fields["evidence"]
    assert evidence["secret_type"] == "AWS Access Key ID" and evidence["validity"] == "active"
    assert evidence["assets"] == ["filigran/portal", "991", "github.com:repository:991"]
    assert "AKIA-never-read" not in repr(fields)


def test_a_code_scanning_alert_is_a_path_3_signal_on_its_repository() -> None:
    row, fields = _match("github", "code_scanning_alert.created", _code())
    assert (row.trigger_path, row.source) == ("code_flaw", "code_scanning")
    assert (fields["external_id"], fields["ref"]) == ("991:code:7", "js/sql-injection")
    evidence = fields["evidence"]
    assert evidence["severity"] == "high" and evidence["tool"] == "CodeQL"
    assert evidence["affected_file"] == "src/db.js"


def test_an_indicator_sighting_is_a_path_2_signal_inside_and_a_vulnerability_sighting_stays_path_1() -> None:
    data: dict[str, Any] = {
        "type": "sighting",
        "id": "sighting--1",
        "sighting_of_ref": "indicator--abc",
        "where_sighted_refs": ["identity--filigran"],
        "count": 3,
    }
    payload = {"type": "create", "data": data}
    row, fields = _match("opencti", "create", payload)
    assert (row.trigger_path, row.lens, row.source) == ("relevant_threat", "inside", "indicator_sighting")
    assert (fields["external_id"], fields["ref"]) == ("sighting--1", "indicator--abc")
    assert fields["evidence"]["where_sighted"] == ["identity--filigran"] and fields["evidence"]["count"] == 3
    data["sighting_of_ref"] = "vulnerability--x"
    row, _ = _match("opencti", "create", payload)
    assert row.trigger_path == "new_vulnerability"


@pytest.mark.parametrize(
    ("event", "resource", "path"),
    [("finding_created", "FINDING", "new_exposure"), ("asset_created", "ASSET", "surface_drift")],
)
def test_an_openaev_finding_or_new_asset_is_recorded_on_its_path(event: str, resource: str, path: str) -> None:
    payload = {"_openaev_event": {"event_type": event, "resource_type": resource, "resource_id": "r-1", "message": "m"}}
    row, fields = _match("openaev", event, payload)
    assert (row.trigger_path, row.source) == (path, event)
    assert fields["external_id"] == "r-1"


async def test_a_new_source_records_nothing_until_it_is_included(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    recorded: list[dict[str, Any]] = []

    async def _excluded(_db: object, _org: object) -> dict[str, set[tuple[str, ...]]]:
        return sources.parse_excluded(_profile())

    class _Db:
        async def __aenter__(self) -> _Db:
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

    async def _primary(_db: object) -> SimpleNamespace:
        return SimpleNamespace(id="org")

    async def _record(*_a: object, **_k: Any) -> None:
        recorded.append(_k)
        raise RuntimeError("stop after the gate")

    import app.core.database as database
    import app.services.ctem_signals as ctem_signals
    import app.services.organizations as organizations

    monkeypatch.setattr(database, "async_session", lambda: _Db())
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(sources, "excluded_sources", _excluded)
    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    assert await listeners.note_event("github", "code_scanning_alert.created", _code(), verified=True) is False
    assert recorded == []

    async def _included(_db: object, _org: object) -> dict[str, set[tuple[str, ...]]]:
        return sources.parse_excluded(_profile(included={"code_flaw": ["github:listener:code_scanning"]}))

    monkeypatch.setattr(sources, "excluded_sources", _included)
    # A profile save forgets the cached exclusion map (#2583 point 7).
    sources.forget_exclusions()
    await listeners.note_event("github", "code_scanning_alert.created", _code(), verified=True)
    assert len(recorded) == 1 and recorded[0]["signal"].trigger_path == "code_flaw"


async def test_an_excluded_delivery_opens_no_session_once_the_exclusions_are_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2583 point 7: the per-path exclusion is decided from the cached map before any database session."""
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    opened: list[int] = []

    def _session() -> Any:
        opened.append(1)
        raise AssertionError("an excluded delivery must not open a session")

    sources.remember_exclusions("org", sources.parse_excluded(_profile()))
    assert (
        await listeners.note_event(
            "github", "code_scanning_alert.created", _code(), verified=True, session_factory=_session
        )
        is False
    )
    assert opened == []
    sources.forget_exclusions()
    assert sources.cached_exclusions() is None


async def test_a_resolved_secret_or_a_fixed_code_alert_closes_its_members(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    closed: list[tuple[str, str]] = []

    async def _close(provider: str, external_id: str) -> int:
        closed.append((provider, external_id))
        return 1

    monkeypatch.setattr(listeners, "_close_at_source", _close)
    await listeners.note_event("github", "secret_scanning_alert.resolved", _secret("resolved"), verified=True)
    await listeners.note_event("github", "code_scanning_alert.fixed", _code("fixed"), verified=True)
    assert closed == [("github", "991:secret:4"), ("github", "991:code:7")]
