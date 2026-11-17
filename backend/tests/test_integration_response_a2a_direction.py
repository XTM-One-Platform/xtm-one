"""The integration response states an A2A integration's direction (#11790).

The integration page used to infer it by calling the outbound-only
``/a2a/client/{id}`` endpoints, which answer 404 for an inbound integration.
The response now carries ``a2a_direction`` from the settings row: one query
for a whole listing, none when the page holds no A2A integration.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.a2a.providers import OAUTH_A2A_PROVIDER
from app.api.v1 import integrations
from app.api.v1.schemas.integration import IntegrationResponse
from app.models.a2a import DIRECTION_INBOUND


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    """Answers the one query ``directions_of`` makes, and counts it."""

    def __init__(self, directions: dict[uuid.UUID, str]):
        self.directions = directions
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        return _Result(list(self.directions.items()))


def _integration(provider: str):
    return SimpleNamespace(id=uuid.uuid4(), provider=provider)


@pytest.mark.asyncio
async def test_an_a2a_integration_gets_its_direction_in_one_query():
    a2a, github = _integration(OAUTH_A2A_PROVIDER), _integration("github")
    db = _Session({a2a.id: DIRECTION_INBOUND})
    assert await integrations._a2a_directions(db, [a2a, github]) == {str(a2a.id): DIRECTION_INBOUND}
    assert db.queries == 1


@pytest.mark.asyncio
async def test_a_page_with_no_a2a_integration_makes_no_query():
    db = _Session({})
    assert await integrations._a2a_directions(db, [_integration("github")]) == {}
    assert db.queries == 0


def test_the_response_carries_the_field_and_defaults_to_none():
    field = IntegrationResponse.model_fields["a2a_direction"]
    assert field.default is None
