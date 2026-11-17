import pytest
from fastapi import HTTPException

from app.api.v1 import integration_elements
from app.api.v1.integration_elements import _list_provider_elements
from app.integrations.providers.microsoft import GRAPH_API


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    last_instance = None

    def __init__(self, *_args, **_kwargs) -> None:
        _FakeAsyncClient.last_instance = self
        self.calls: list[tuple[str, dict]] = []
        self._responses = {
            f"{GRAPH_API}/me/joinedTeams": _FakeResponse(
                200,
                {
                    "value": [{"id": "team-1", "displayName": "Alpha"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/joinedTeams?page=2",
                },
            ),
            "https://graph.microsoft.com/v1.0/me/joinedTeams?page=2": _FakeResponse(
                200, {"value": [{"id": "team-2", "displayName": "Beta"}]}
            ),
            f"{GRAPH_API}/teams/team-1/channels": _FakeResponse(
                200,
                {
                    "value": [{"id": "channel-1", "displayName": "General"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/teams/team-1/channels?page=2",
                },
            ),
            "https://graph.microsoft.com/v1.0/teams/team-1/channels?page=2": _FakeResponse(
                200,
                {
                    "value": [
                        {
                            "id": "channel-2",
                            "displayName": "Platform",
                            "membershipType": "private",
                        }
                    ]
                },
            ),
            f"{GRAPH_API}/teams/team-2/channels": _FakeResponse(
                200, {"value": [{"id": "channel-3", "displayName": "Ops"}]}
            ),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, **_kwargs):
        self.calls.append((url, _kwargs))
        if url not in self._responses:
            raise AssertionError(f"Unexpected URL called in test: {url}")
        return self._responses[url]


@pytest.mark.asyncio
async def test_microsoft_teams_channel_listing_handles_graph_pagination(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    elements = await _list_provider_elements("microsoft", "teams_channel", "token")

    assert [el["id"] for el in elements] == [
        "team-1:channel-1",
        "team-1:channel-2",
        "team-2:channel-3",
    ]
    assert elements[1]["metadata"]["membership_type"] == "private"

    calls = _FakeAsyncClient.last_instance.calls
    assert len(calls) == 5
    first_joined_teams_params = calls[0][1]["params"]
    second_joined_teams_params = calls[1][1]["params"]
    first_channels_params = calls[2][1]["params"]
    second_channels_params = calls[3][1]["params"]
    assert "$top" not in first_joined_teams_params
    # The /teams/{id}/channels endpoint rejects $top with HTTP 400,
    # so the channel listing must NOT send it.
    assert "$top" not in first_channels_params
    assert second_joined_teams_params is None
    assert second_channels_params is None


# ── End-to-end error-path coverage through the real entrypoint ──────────


class _FakeJoinedTeamsErrorClient:
    """Returns a Graph-style 4xx error on the very first
    ``me/joinedTeams`` request — picker UIs treat the listing endpoints
    as best-effort fallbacks, so a Graph permission/auth error must
    surface as an empty list (so the picker just shows nothing) and
    produce one structured warning log line — never a 5xx that would
    block the whole settings page.
    """

    last_instance = None

    def __init__(self, *_args, **_kwargs) -> None:
        _FakeJoinedTeamsErrorClient.last_instance = self
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, **_kwargs):
        self.calls.append((url, _kwargs))
        return _FakeResponse(
            403,
            {
                "error": {
                    "code": "Forbidden",
                    "message": "Insufficient privileges to complete the operation.",
                }
            },
            text='{"error":{"code":"Forbidden"}}',
        )


@pytest.mark.asyncio
async def test_microsoft_teams_channel_listing_returns_empty_on_first_page_403(
    monkeypatch, caplog
):
    """Regression: a 403 on the very first Graph page MUST NOT raise —
    it must short-circuit, log a warning containing the error_prefix
    + HTTP status + Graph error message, and return ``[]`` so the
    picker degrades gracefully.

    This is the *end-to-end* version of the error-branch coverage; the
    direct unit-level coverage on ``paginate_graph_collection`` lives
    in ``tests/test_graph_pagination.py``.  Having both layers means
    a regression has to slip past *two* tests to reach production.
    """
    import logging

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeJoinedTeamsErrorClient)

    with caplog.at_level(logging.WARNING):
        elements = await _list_provider_elements("microsoft", "teams_channel", "token")

    # No teams discovered → no channels fetched → empty picker payload.
    assert elements == []
    # Only the very first request (joined-teams) fired before short-circuit;
    # zero channel requests must follow.
    calls = _FakeJoinedTeamsErrorClient.last_instance.calls
    assert len(calls) == 1
    assert calls[0][0].endswith("/me/joinedTeams")
    # The HTTP status + the Graph error message both surface in the
    # structured log line so operators have an actionable trail
    # (no swallowed 403).
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HTTP 403" in r.getMessage() for r in warning_records), (
        f"expected an HTTP 403 warning log line; got: "
        f"{[r.getMessage() for r in warning_records]!r}"
    )
    assert any(
        "Insufficient privileges" in r.getMessage() for r in warning_records
    )


@pytest.mark.asyncio
async def test_list_provider_elements_rejects_whitespace_token():
    with pytest.raises(HTTPException) as exc:
        await _list_provider_elements("microsoft", "teams_channel", "   ")

    # 403, never 401: the app reads a 401 as its own session ending (#11106).
    assert exc.value.status_code == 403
    assert "missing or expired" in str(exc.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access_token_enc", "api_key_enc", "decrypted_token"),
    [
        ("encrypted-access-token", None, "   "),
        (None, None, None),
    ],
)
async def test_list_integration_elements_rejects_missing_or_blank_token(
    monkeypatch, access_token_enc, api_key_enc, decrypted_token
):
    integration = type(
        "Integration",
        (),
        {
            "provider": "microsoft",
            "access_token_enc": access_token_enc,
            "api_key_enc": api_key_enc,
        },
    )()

    async def _fake_get_user_integration(*_args, **_kwargs):
        return integration

    monkeypatch.setattr(
        integration_elements, "get_user_integration", _fake_get_user_integration
    )
    if decrypted_token is not None:
        monkeypatch.setattr(
            integration_elements, "decrypt_value", lambda _v: decrypted_token
        )

    provider_listing_called = False

    async def _fake_list_provider_elements(*_args, **_kwargs):
        nonlocal provider_listing_called
        provider_listing_called = True
        return []

    monkeypatch.setattr(
        integration_elements, "_list_provider_elements", _fake_list_provider_elements
    )

    user = type("User", (), {"id": "user-1", "is_admin": False})()

    with pytest.raises(HTTPException) as exc:
        await integration_elements.list_integration_elements(
            integration_id="integration-1",
            element_type="teams_channel",
            user=user,
            db=None,
        )

    assert exc.value.status_code == 403
    assert "missing or expired" in str(exc.value.detail)
    assert provider_listing_called is False


def test_integration_elements_never_answers_401():
    """A 401 signs the user out of the app (frontend/src/lib/api.ts reads it as
    the session ending), so an integration credential problem is never a 401
    on this endpoint: the trigger editor signed its user out (#11106)."""
    import inspect
    import re

    source = inspect.getsource(integration_elements)
    assert integration_elements.INTEGRATION_CREDENTIAL_UNUSABLE_STATUS == 403
    assert not re.search(r"status_code\s*=\s*401", source)
    assert "HTTP_401" not in source
