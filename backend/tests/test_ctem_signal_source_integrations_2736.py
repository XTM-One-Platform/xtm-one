# SPDX-License-Identifier: Apache-2.0

"""#2736: each signal source names the integrations it reads, and an integration taken off a source records nothing."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signal_sources as sources
from app.services.organization_context import SignalSourcesSettings

PROD = str(uuid.uuid4())
LAB = str(uuid.uuid4())


def _profile(excluded_integrations: dict[str, list[str]]) -> SimpleNamespace:
    return SimpleNamespace(signal_sources=SignalSourcesSettings(excluded_integrations=excluded_integrations))


def _dependabot() -> dict[str, Any]:
    from tests.test_ctem_signals import _dependabot as delivery

    payload = delivery("created")
    assert listeners.match("github", "dependabot_alert.created", payload) is not None
    return payload


def test_the_setting_keeps_known_sources_and_integration_ids_only() -> None:
    clean = SignalSourcesSettings(
        excluded_integrations={"github:listener": [PROD.upper(), PROD, "oauth_default"], "aikido:listener": []}
    )
    assert clean.excluded_integrations == {"github:listener": sorted([PROD, "oauth_default"])}
    with pytest.raises(ValueError):
        SignalSourcesSettings(excluded_integrations={"nope:listener": [PROD]})
    with pytest.raises(ValueError):
        SignalSourcesSettings(excluded_integrations={"github:listener": ["not-an-id"]})


def test_an_absent_setting_uses_every_integration() -> None:
    assert sources.parse_excluded_integrations(SimpleNamespace(signal_sources=SignalSourcesSettings())) == {}
    assert sources.parse_excluded_integrations(None) == {}
    excluded = sources.parse_excluded_integrations(_profile({"github:listener": [LAB]}))
    assert sources.integration_excluded(excluded, "github", "listener", "", LAB)
    assert not sources.integration_excluded(excluded, "github", "listener", "", PROD)
    # A named source of the same provider keeps every integration: the exclusion is per source.
    assert not sources.integration_excluded(excluded, "github", "listener", "code_scanning", LAB)
    # A delivery whose integration is unknown is never filtered.
    assert not sources.integration_excluded(excluded, "github", "listener", "", None)


def test_path_sources_lists_each_integration_with_its_switch() -> None:
    bound = sources.BoundSources({("github", "listener")})
    bound.integrations = {
        "github": [
            {
                "id": PROD,
                "name": "GitHub prod",
                "status": "connected",
                "webhook_secret_set": True,
                "last_delivery_at": None,
            },
            {
                "id": LAB,
                "name": "GitHub lab",
                "status": "connected",
                "webhook_secret_set": True,
                "last_delivery_at": None,
            },
        ]
    }
    bound.excluded_integrations = {"github:listener": {LAB}}
    row = next(s for s in sources.path_sources("code_flaw", bound) if s["key"] == "github:listener")
    assert [(i["name"], i["used"]) for i in row["integrations"]] == [("GitHub prod", True), ("GitHub lab", False)]
    # Without integrations read (the agent tool), the list is empty, never missing.
    assert (
        next(s for s in sources.path_sources("code_flaw", set()) if s["key"] == "github:listener")["integrations"] == []
    )


async def test_a_delivery_of_an_excluded_integration_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    recorded: list[dict[str, Any]] = []

    class _Db:
        async def __aenter__(self) -> _Db:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def _primary(_db: object) -> SimpleNamespace:
        return SimpleNamespace(id="org")

    async def _no_exclusions(_db: object, _org: object) -> dict[str, set[tuple[str, ...]]]:
        return {}

    async def _by_integration(_db: object, _org: object) -> dict[str, set[str]]:
        return sources.parse_excluded_integrations(_profile({"github:listener": [LAB]}))

    async def _record(_db: object, **kw: Any) -> tuple[SimpleNamespace, bool]:
        recorded.append(kw)
        return SimpleNamespace(), True

    import app.core.database as database
    import app.services.ctem_signals as ctem_signals
    import app.services.organizations as organizations

    monkeypatch.setattr(database, "async_session", lambda: _Db())
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(sources, "excluded_sources", _no_exclusions)
    monkeypatch.setattr(sources, "excluded_integrations", _by_integration)
    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    sources.forget_exclusions()
    try:
        assert (
            await listeners.note_event(
                "github", "dependabot_alert.created", _dependabot(), verified=True, integration_id=LAB
            )
            is False
        )
        assert recorded == []
        # Cached now: the next excluded delivery is dropped before any session.
        assert sources.cached_integration_exclusions() is not None
        await listeners.note_event(
            "github", "dependabot_alert.created", _dependabot(), verified=True, integration_id=PROD
        )
        assert len(recorded) == 1
        # No integration named (a caller that does not know it): recorded, as before.
        await listeners.note_event("github", "dependabot_alert.created", _dependabot(), verified=True)
        assert len(recorded) == 2
    finally:
        sources.forget_exclusions()
    assert sources.cached_integration_exclusions() is None


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    def __call__(self) -> _Recorder:
        return self

    async def __aenter__(self) -> _Recorder:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    async def commit(self) -> None:
        self.commits += 1


async def test_a_verified_delivery_stamps_its_integration_once_a_minute() -> None:
    sources.forget_deliveries()
    db = _Recorder()
    try:
        assert await sources.note_delivery("github", PROD.upper(), session_factory=db) is True
        # A second delivery within the minute writes nothing: a storm costs one upsert.
        assert await sources.note_delivery("github", PROD, session_factory=db) is False
        # Another integration, or the provider route, is stamped apart.
        assert await sources.note_delivery("github", "oauth_default", session_factory=db) is True
        assert await sources.note_delivery("github", None, session_factory=db) is False
        assert len(db.statements) == 2 and db.commits == 2
        params = db.statements[0].compile().params
        assert params["provider"] == "github" and params["integration_id"] == PROD
        assert "ON CONFLICT" in str(db.statements[0].compile(dialect=_pg_dialect()))
    finally:
        sources.forget_deliveries()


async def test_a_failed_stamp_never_raises_and_is_retried() -> None:
    sources.forget_deliveries()

    class _Broken(_Recorder):
        async def execute(self, statement: Any) -> None:
            raise RuntimeError("database down")

    try:
        assert await sources.note_delivery("aikido", LAB, session_factory=_Broken()) is False
        db = _Recorder()
        assert await sources.note_delivery("aikido", LAB, session_factory=db) is True
    finally:
        sources.forget_deliveries()


async def test_note_event_stamps_a_verified_delivery_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    stamped: list[tuple[str, str | None]] = []

    async def _stamp(provider: str, integration_id: str | None, **_kw: Any) -> bool:
        stamped.append((provider, integration_id))
        return True

    monkeypatch.setattr(sources, "note_delivery", _stamp)
    # An event no listener maps still proves the integration delivers.
    await listeners.note_event("github", "push", {}, verified=True, integration_id=PROD)
    await listeners.note_event("github", "push", {}, verified=False, integration_id=LAB)
    await listeners.note_event("github", "push", {}, verified=True)
    assert stamped == [("github", PROD)]


def _pg_dialect() -> Any:
    from sqlalchemy.dialects import postgresql

    return postgresql.dialect()  # type: ignore[no-untyped-call]
