import functools
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from app.api.v1 import mcp_auth, mcp_endpoint


class _CandidatesResult:
    def __init__(self, candidates):
        self._candidates = candidates

    def scalars(self):
        return self

    def all(self):
        return self._candidates


class _UserResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _FakeSession:
    def __init__(self, candidate_result, user_result):
        self._candidate_result = candidate_result
        self._user_result = user_result
        self._calls = 0

    async def execute(self, _statement):
        self._calls += 1
        if self._calls == 1:
            return self._candidate_result
        if self._calls == 2:
            return self._user_result
        raise AssertionError("Unexpected execute() call: the key's use is buffered in Redis, never written here")


@pytest.mark.asyncio
async def test_authenticate_user_continues_when_usage_tracking_fails(monkeypatch):
    """The key's use is buffered in Redis: a store that does not answer costs the count, never the call."""
    from app.services import api_key_access

    token = "fcp-1234567890abcdefghij"
    candidate = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="CI",
        key_prefix=token[:12] + "...",
        key_hash="unused",
        is_active=True,
        surfaces=None,
        read_only=False,
        agent_ids=None,
        expires_at=None,
        rate_limit_per_minute=None,
        quota_limit=None,
        quota_period=None,
    )
    user = SimpleNamespace(id=candidate.user_id, is_active=True)
    db = _FakeSession(_CandidatesResult([candidate]), _UserResult(user))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/user",
            "headers": [(b"x-api-key", token.encode("utf-8"))],
            "query_string": b"",
        }
    )

    async def _fake_try_oauth_token(*_args, **_kwargs):
        return None

    async def _fake_checkpw_async(*_args, **_kwargs):
        return True

    async def _redis_down():
        raise SQLAlchemyError("not the store this time either")

    monkeypatch.setattr(mcp_auth, "try_oauth_token", _fake_try_oauth_token)
    monkeypatch.setattr(api_key_access, "bcrypt_checkpw_async", _fake_checkpw_async)
    monkeypatch.setattr(api_key_access, "redis_client", _redis_down)

    authenticated_user = await mcp_auth.authenticate_user(
        request=request,
        db=db,  # type: ignore[arg-type]
        resource_path="mcp/user",
    )

    assert authenticated_user is user
    assert api_key_access.principal_of(user).key_id == candidate.id


# ── RFC 8707 audience binding ────────────────────────────────────────────────
#
# The MCP spec requires clients to send the ``resource`` parameter, and
# ``/mcp/oauth`` copies it into the access token's ``aud``. PyJWT *rejects* any
# token carrying ``aud`` unless ``decode`` is given an ``audience``, so a
# resource server that ignores the claim turns every spec-compliant client away
# with a blanket 401 — which is exactly what happened the first time this
# platform's own MCP client started sending ``resource``. These tests pin both
# halves: an audience naming this resource is accepted, one naming another is
# refused, and a token with no ``aud`` at all still works.


class _SingleUserSession:
    """Minimal session that always resolves the user lookup."""

    def __init__(self, user):
        self._user = user

    async def execute(self, _statement):
        return _UserResult(self._user)


def _mcp_oauth_token(**claims):
    import jwt

    from app.api.v1.mcp_oauth import MCP_OAUTH_TOKEN_TYPE
    from app.core.config import settings

    payload = {"sub": str(uuid.uuid4()), "type": MCP_OAUTH_TOKEN_TYPE}
    payload.update(claims)
    return payload, jwt.encode(
        payload, settings.secret_key, algorithm=settings.jwt_algorithm
    )


def _active_user(user_id):
    return SimpleNamespace(id=uuid.UUID(user_id), is_active=True)


@pytest.mark.asyncio
async def test_oauth_token_accepted_when_audience_names_this_resource():
    from app.core.config import settings

    resource = f"{settings.base_url.rstrip('/')}/mcp/user"
    payload, token = _mcp_oauth_token(aud=resource)
    session = _SingleUserSession(_active_user(payload["sub"]))

    user = await mcp_auth.try_oauth_token(token, session, "mcp/user")

    assert user is not None, (
        "a token minted for this resource must authenticate — rejecting it "
        "locks out every MCP client that sends the required resource parameter"
    )


@pytest.mark.asyncio
async def test_oauth_token_refused_when_audience_names_another_resource():
    """Audience binding is the entire point of the resource parameter."""
    from app.core.config import settings

    payload, token = _mcp_oauth_token(
        aud=f"{settings.base_url.rstrip('/')}/mcp/opencti"
    )
    session = _SingleUserSession(_active_user(payload["sub"]))

    assert await mcp_auth.try_oauth_token(token, session, "mcp/user") is None


@pytest.mark.asyncio
async def test_oauth_token_without_audience_still_authenticates():
    """Backward compatibility: tokens minted before audience binding existed."""
    payload, token = _mcp_oauth_token()
    session = _SingleUserSession(_active_user(payload["sub"]))

    assert await mcp_auth.try_oauth_token(token, session, "mcp/user") is not None


@pytest.mark.asyncio
async def test_audience_comparison_tolerates_a_trailing_slash():
    from app.core.config import settings

    resource = f"{settings.base_url.rstrip('/')}/mcp/user/"
    payload, token = _mcp_oauth_token(aud=[resource])
    session = _SingleUserSession(_active_user(payload["sub"]))

    assert await mcp_auth.try_oauth_token(token, session, "mcp/user") is not None


# ── Each endpoint is bound to its own resource ───────────────────────────────
#
# /mcp/global exposes the admin-selected agents platform-wide, so it is the
# more sensitive of the two. It authenticates OAuth tokens as well as `mcp-`
# API keys, and it must judge them against its own identity: sharing the user
# endpoint's would refuse tokens legitimately minted for /mcp/global *and*
# accept /mcp/user tokens on it — the cross-resource replay the audience check
# exists to stop.


@pytest.mark.asyncio
async def test_global_endpoint_accepts_a_token_minted_for_itself():
    from app.core.config import settings

    payload, token = _mcp_oauth_token(
        aud=f"{settings.base_url.rstrip('/')}/mcp/global"
    )
    session = _SingleUserSession(_active_user(payload["sub"]))

    assert (
        await mcp_auth.try_oauth_token(token, session, "mcp/global") is not None
    )


@pytest.mark.asyncio
async def test_global_endpoint_refuses_a_user_endpoint_token():
    from app.core.config import settings

    payload, token = _mcp_oauth_token(
        aud=f"{settings.base_url.rstrip('/')}/mcp/user"
    )
    session = _SingleUserSession(_active_user(payload["sub"]))

    assert await mcp_auth.try_oauth_token(token, session, "mcp/global") is None


@pytest.mark.asyncio
async def test_user_endpoint_refuses_a_global_endpoint_token():
    from app.core.config import settings

    payload, token = _mcp_oauth_token(
        aud=f"{settings.base_url.rstrip('/')}/mcp/global"
    )
    session = _SingleUserSession(_active_user(payload["sub"]))

    assert await mcp_auth.try_oauth_token(token, session, "mcp/user") is None


def test_resource_path_has_no_default():
    """A default would silently re-open the hole for the next call site.

    The global endpoint originally inherited "mcp/user" precisely because the
    parameter could be omitted.
    """
    import inspect

    for helper in (mcp_auth.try_oauth_token, mcp_auth.authenticate_user):
        param = inspect.signature(helper).parameters["resource_path"]
        assert param.default is inspect.Parameter.empty, helper.__name__


@pytest.mark.asyncio
async def test_global_endpoint_call_site_passes_its_own_resource(monkeypatch):
    """Covers the call site, not just the helper.

    The audience tests above pass ``resource_path`` explicitly, so they keep
    passing even when the endpoint forgets to — which is exactly how the
    original defect survived. This drives ``_authenticate_global_endpoint``
    itself and records what it asked for.
    """
    import app.api.v1.platform_settings_helpers as ps_helpers
    from app.core.config import settings

    seen: dict[str, object] = {}

    async def _spy(_token, _db, resource_path, **kwargs):
        seen["resource_path"] = resource_path
        seen["require_audience"] = kwargs.get("require_audience")
        return None  # force the API-key fallback so the flow ends predictably

    async def _fake_load_ps(_db):
        return SimpleNamespace(mcp_global_api_key_hash=None)

    monkeypatch.setattr(mcp_endpoint, "try_oauth_token", _spy)
    monkeypatch.setattr(ps_helpers, "get_platform_settings", _fake_load_ps)

    _, token = _mcp_oauth_token(aud=f"{settings.base_url.rstrip('/')}/mcp/user")
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/global",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        }
    )

    # No global key configured, so the fallback ends in a 401 — the point
    # here is what the OAuth attempt was given, not how the request ends.
    with pytest.raises(HTTPException) as excinfo:
        await mcp_endpoint._authenticate_global_endpoint(request=request, db=None)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 401

    assert seen.get("resource_path") == "mcp/global", (
        "the global endpoint must judge tokens against its own resource; "
        "inheriting mcp/user would accept a user-endpoint token here"
    )
    assert seen.get("require_audience") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["user", "global"])
@pytest.mark.parametrize(
    ("version_header", "required"),
    [(None, False), ("2025-11-25", False), ("2026-07-28", True)],
    ids=["no-header", "handshake-revision", "modern-revision"],
)
async def test_both_endpoints_require_an_audience_on_a_modern_request(monkeypatch, endpoint, version_header, required):
    """Pinned at the call sites: dropping ``require_audience=is_modern_request(...)``
    at either one would silently accept audience-less tokens on 2026-07-28 requests."""
    import app.api.v1.platform_settings_helpers as ps_helpers

    seen: dict[str, object] = {}

    async def _spy(_token, _db, resource_path, **kwargs):
        seen["require_audience"] = kwargs.get("require_audience")
        return None

    async def _fake_load_ps(_db):
        return SimpleNamespace(mcp_global_api_key_hash=None)

    monkeypatch.setattr(mcp_auth, "try_oauth_token", _spy)
    monkeypatch.setattr(mcp_endpoint, "try_oauth_token", _spy)
    monkeypatch.setattr(ps_helpers, "get_platform_settings", _fake_load_ps)
    headers = [(b"authorization", b"Bearer not-an-api-key")]
    if version_header is not None:
        headers.append((b"mcp-protocol-version", version_header.encode()))
    request = Request(
        {"type": "http", "method": "POST", "path": f"/mcp/{endpoint}", "headers": headers, "query_string": b""}
    )

    authenticate = (
        functools.partial(mcp_auth.authenticate_user, resource_path="mcp/user")
        if endpoint == "user"
        else mcp_endpoint._authenticate_global_endpoint
    )
    with pytest.raises(HTTPException) as excinfo:
        await authenticate(request=request, db=None)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 401
    assert seen == {"require_audience": required}
