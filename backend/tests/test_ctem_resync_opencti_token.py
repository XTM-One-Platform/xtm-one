# SPDX-License-Identifier: Apache-2.0

"""#2615: a purge resync outlives the 10-minute OpenCTI JWT, and every route says why it records nothing."""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_signal_resync as resync
from app.services import opencti_trigger_provisioning as prov

NOW = datetime(2026, 11, 27, 12, 31, tzinfo=UTC)


def _jwt(exp: float) -> str:
    def _part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{_part({'alg': 'EdDSA'})}.{_part({'exp': exp})}.sig"


class _Response:
    status_code = 200

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


_AUTH_REQUIRED = {"errors": [{"message": "You must be logged in to do this.", "extensions": {"code": "AUTH_REQUIRED"}}]}


def _platform_gql(monkeypatch: pytest.MonkeyPatch, token: str, minted: list[str], fresh: str) -> list[str]:
    """``integration_gql`` on a platform-managed integration whose re-minter answers *fresh*."""
    sent: list[str] = []

    async def _get_token(db: Any, integration_id: str, provider: str, **kw: Any) -> str:
        return f"https://opencti.example||{token}"

    def _refresh_fn(integration_id: str, user_id: str | None) -> Any:
        async def _mint() -> str:
            minted.append(user_id or "")
            return fresh

        return _mint

    async def _safe_request(client: Any, method: str, url: str, *a: Any, headers: Any, **kw: Any) -> Any:
        auth = headers["Authorization"]
        sent.append(auth)
        if auth == f"Bearer {fresh}":
            return _Response({"data": {"stixCoreObjects": {"edges": []}}}), None
        return _Response(_AUTH_REQUIRED), None

    async def _sendable(headers: Any, query: str) -> None:
        return None

    monkeypatch.setattr("app.services.integration_tools._get_token", _get_token)
    monkeypatch.setattr("app.services.integration_tools._make_platform_jwt_refresh_fn", _refresh_fn)
    monkeypatch.setattr("app.integrations.providers.opencti._common.safe_request", _safe_request)
    monkeypatch.setattr("app.integrations.providers.opencti.capabilities.unsent_reason", _sendable)
    return sent


_INTEGRATION = SimpleNamespace(id=uuid.uuid4(), auth_type="platform_managed")


@pytest.mark.asyncio
async def test_a_jwt_about_to_expire_is_reminted_before_the_route_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resync past the token lifetime keeps reading: the next page goes out on a fresh JWT."""
    minted: list[str] = []
    fresh = _jwt(time.time() + 600)
    sent = _platform_gql(monkeypatch, _jwt(time.time() + 30), minted, fresh)
    connection = await prov.integration_gql(None, _INTEGRATION, "admin-1")
    assert not isinstance(connection, str)
    gql, _ = connection
    data, err = await gql(resync._QUERY, {"types": ["Intrusion-Set"], "first": 200, "after": None})
    assert err is None and data == {"stixCoreObjects": {"edges": []}}
    assert sent == [f"Bearer {fresh}"] and minted == ["admin-1"]
    # The fresh token stays on the shared headers: the following routes reuse it, no second mint.
    await gql(resync._QUERY, {"types": ["Malware"], "first": 200, "after": None})
    assert sent[-1] == f"Bearer {fresh}" and minted == ["admin-1"]


@pytest.mark.asyncio
async def test_an_auth_required_answer_is_reminted_once_and_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCTI answers an expired JWT with a 200 AUTH_REQUIRED: the call re-mints once and reads."""
    minted: list[str] = []
    sent = _platform_gql(monkeypatch, "expired-token", minted, "fresh-token")
    connection = await prov.integration_gql(None, _INTEGRATION, "admin-1")
    assert not isinstance(connection, str)
    data, err = await connection[0](resync._QUERY, {"types": ["Campaign"], "first": 200, "after": None})
    assert err is None and data == {"stixCoreObjects": {"edges": []}}
    assert sent == ["Bearer expired-token", "Bearer fresh-token"] and minted == ["admin-1"]


@pytest.mark.asyncio
async def test_a_static_key_integration_gets_no_reminter(monkeypatch: pytest.MonkeyPatch) -> None:
    minted: list[str] = []
    _platform_gql(monkeypatch, "static-key", minted, "never")
    connection = await prov.integration_gql(None, SimpleNamespace(id=uuid.uuid4(), auth_type="api_key"), "u1")
    assert not isinstance(connection, str)
    _, err = await connection[0](resync._QUERY, {"types": ["Malware"], "first": 200, "after": None})
    assert err and minted == []


# ── why a route records nothing ─────────────────────────────────────────


def test_each_world_type_names_the_path_its_listener_records_on() -> None:
    assert resync.listener_path("Vulnerability") == "new_vulnerability"
    assert resync.listener_path("Intrusion-Set") == "relevant_threat"
    assert resync.listener_path("Threat-Actor-Individual") == "relevant_threat"
    assert resync.listener_path("Email-Addr") == "credential_leak"


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _world(monkeypatch: pytest.MonkeyPatch, gql: Any, off: set[str]) -> None:
    async def _sources(db: Any) -> list[Any]:
        return [SimpleNamespace(integration_id="i1", run_as_user_id="u1")]

    async def _conn(db: Any, integration_id: str, user_id: Any) -> Any:
        return gql, integration_id, "secret"

    async def _off(sessions: Any) -> set[str]:
        return off

    monkeypatch.setattr(resync, "_opencti_sources", _sources)
    monkeypatch.setattr(resync, "_excluded_listener_paths", _off)
    monkeypatch.setattr("app.services.opencti_trigger_provisioning.integration_connection", _conn)


@pytest.mark.asyncio
async def test_a_route_whose_listener_source_is_off_is_skipped_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2615: 3600 vulnerabilities read and 0 recorded was the opencti:listener source off on path 1."""
    asked: list[str] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        asked.append(variables["types"][0])
        node = {"id": "x", "entity_type": variables["types"][0], "name": "n"}
        return {"stixCoreObjects": {"edges": [{"node": node}], "pageInfo": {"hasNextPage": False}}}, None

    async def _note(*a: Any, **k: Any) -> bool:
        return True

    _world(monkeypatch, _gql, {"new_vulnerability"})
    routes = await resync.resync_world(lambda: _Session(), now=NOW, note=_note)
    assert routes["vulnerability"]["skipped"] == resync.LISTENER_OFF and routes["vulnerability"]["read"] == 0
    assert "Vulnerability" not in asked
    assert routes["intrusion_set"]["read"] == 1 and routes["intrusion_set"]["recorded"] == 1


@pytest.mark.asyncio
async def test_the_off_paths_are_read_from_the_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _primary(db: Any) -> Any:
        return SimpleNamespace(id="org-1")

    async def _excluded(db: Any, organization_id: Any) -> dict[str, set[tuple[str, ...]]]:
        return {"new_vulnerability": {("opencti", "listener")}, "relevant_threat": {("opencti", "pir_playbook")}}

    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr("app.services.ctem_signal_sources.excluded_sources", _excluded)
    assert await resync._excluded_listener_paths(lambda: _Session()) == {"new_vulnerability"}


@pytest.mark.asyncio
async def test_a_refused_token_is_one_short_line_per_route_and_not_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    long = (
        "GraphQL error in 'CtemResync': You must be logged in to do this. [AUTH_REQUIRED] "
        '{"http_status": 401} — unauthorized: the OpenCTI API token was rejected'
    )

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], str]:
        asked.append(variables["types"][0])
        return {}, long

    async def _note(*a: Any, **k: Any) -> bool:
        return True

    _world(monkeypatch, _gql, set())
    routes = await resync.resync_world(lambda: _Session(), now=NOW, note=_note)
    assert asked == ["Vulnerability"]
    for key in ("vulnerability", "intrusion_set", "malware", "email_addr"):
        assert routes[key]["errors"] == [resync.AUTH_REFUSED]
    assert len(resync.AUTH_REFUSED) < 120


@pytest.mark.asyncio
async def test_a_refused_connection_does_not_mark_a_healthy_one_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2619 review: connection 0 refused must leave connection 1 read on every later route."""
    healthy: list[str] = []

    async def _refused(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], str]:
        return {}, "You must be logged in to do this. [AUTH_REQUIRED]"

    async def _ok(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        healthy.append(variables["types"][0])
        node = {"id": "x", "entity_type": variables["types"][0], "name": "n"}
        return {"stixCoreObjects": {"edges": [{"node": node}], "pageInfo": {"hasNextPage": False}}}, None

    async def _connections(sessions: Any) -> tuple[list[Any], list[str]]:
        return [_refused, _ok], []

    async def _off(sessions: Any) -> set[str]:
        return set()

    async def _note(*a: Any, **k: Any) -> bool:
        return True

    monkeypatch.setattr(resync, "_opencti_connections", _connections)
    monkeypatch.setattr(resync, "_excluded_listener_paths", _off)
    routes = await resync.resync_world(lambda: _Session(), now=NOW, note=_note)
    world = [w for w in resync.WINDOWS if w.entity_type]
    assert healthy == [w.entity_type for w in world]
    for window in world:
        assert routes[window.key]["read"] == 1
        assert routes[window.key]["errors"] == [resync.AUTH_REFUSED]
