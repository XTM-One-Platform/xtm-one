"""Platform Health's API prefix (#101 S1): ``/api/v1/health`` serves the
former Concierge routes, and ``/api/v1/concierge`` stays a deprecated alias.

Pinned here: every Concierge route template exists under both prefixes, only
the new one is published in the OpenAPI schema, and an answer under the alias
carries ``Deprecation: true`` with a ``Link`` to its successor while an answer
under the new prefix carries neither.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.api.v1 import (
    platform_health,
    platform_health_companion,
    platform_health_cycles,
    platform_health_findings,
)
from app.main import CONCIERGE_ALIAS_PREFIX, HEALTH_API_PREFIX, DeprecatedAliasMiddleware, application


def _suffixes() -> set[str]:
    routers = (
        platform_health.router,
        platform_health_cycles.router,
        platform_health_companion.router,
        platform_health_findings.router,
    )
    return {route.path for router in routers for route in router.routes if isinstance(route, APIRoute)}


def test_every_route_is_served_under_both_prefixes():
    templates = {route.path for route in application.routes if isinstance(route, APIRoute)}
    suffixes = _suffixes()

    assert suffixes
    assert {HEALTH_API_PREFIX + s for s in suffixes} <= templates
    assert {CONCIERGE_ALIAS_PREFIX + s for s in suffixes} <= templates


def test_only_the_new_prefix_is_published():
    published = {route.path for route in application.routes if isinstance(route, APIRoute) and route.include_in_schema}

    assert not any(path.startswith(CONCIERGE_ALIAS_PREFIX + "/") for path in published)
    assert any(path.startswith(HEALTH_API_PREFIX + "/") for path in published)


def _client() -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/api/v1/{rest:path}", ok)])
    return TestClient(DeprecatedAliasMiddleware(app, aliases={CONCIERGE_ALIAS_PREFIX: HEALTH_API_PREFIX}))


@pytest.mark.parametrize("suffix", ["/name", "/pets/owl/spritesheet"])
def test_an_answer_under_the_alias_is_marked_deprecated(suffix):
    response = _client().get(CONCIERGE_ALIAS_PREFIX + suffix)

    assert response.headers["deprecation"] == "true"
    assert response.headers["link"].endswith(f'{HEALTH_API_PREFIX}{suffix}>; rel="successor-version"')


@pytest.mark.parametrize("path", [HEALTH_API_PREFIX + "/name", "/api/v1/conciergex/name", "/api/v1/agents"])
def test_other_answers_are_not_marked(path):
    response = _client().get(path)

    assert "deprecation" not in response.headers
    assert "link" not in response.headers
