"""Skills API validation for private-repository skill sources.

The point of validating the credential binding at create/update time is that
the operator finds out *now* which part is misconfigured, instead of finding a
stale skill and an ERROR log a day later.  These tests pin the rejections and
the happy path of ``create_skill`` / ``update_skill`` / ``refresh_skill``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

import app.api.v1.agent_skills as agent_skills_mod
import app.services.skill_source as skill_source_mod
from app.api.v1.schemas.agent_skill import AgentSkillCreate, AgentSkillUpdate
from app.services.skill_source import SkillCredential

TOKEN = "ghp_APIVALIDATIONTOKEN123456"
INTEGRATION_ID = uuid4()
BLOB_URL = "https://github.com/sopra/agent-skills/blob/main/SKILL.md"
SKILL_BODY = "---\nname: triage\ndescription: from private repo\n---\n# body"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _user(is_admin: bool = False):
    return SimpleNamespace(
        id=uuid4(),
        full_name="Ada Lovelace",
        is_admin=is_admin,
        role="admin" if is_admin else "user",
        email="ada@example.com",
    )


def _integration(
    *, provider: str = "github", status: str = "connected", name: str = "Sopra GitHub"
):
    return SimpleNamespace(
        id=INTEGRATION_ID,
        provider=provider,
        status=status,
        name=name,
        access_token_enc=None,
        api_key_enc="enc",
    )


def _credential(name: str = "Sopra GitHub") -> SkillCredential:
    return SkillCredential(
        token=TOKEN, integration_id=INTEGRATION_ID, integration_name=name
    )


def _skill(**overrides):
    base = {
        "id": uuid4(),
        "user_id": None,
        "url": BLOB_URL,
        "source_integration_id": INTEGRATION_ID,
        "source_ref": None,
        "name": "triage",
        "description": None,
        "compatibility": None,
        "license": None,
        "content": "old body",
        "fetched_at": datetime.now(UTC),
        "refresh_failure_count": 0,
        "auth_failure_count": 0,
        "tags": [],
        "enabled": True,
        "hide_from_chat": False,
        "is_company_managed": False,
        "is_public_catalog": False,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "access_restricted": False,
        "forked_from": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _http_response(status: int, *, text: str = "", headers: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = text
    resp.content = text.encode()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


class _RecordedGet:
    """Records every request the route issued, and replays a fixed response.

    A plain callable rather than an ``AsyncMock``: the assertions below hinge on
    *whether* a request was made and with which headers, so they must not depend
    on mock's magic-method machinery (an ``AsyncMock`` whose ``__aenter__`` is
    replaced by attribute assignment silently yields a *different* object if
    that assignment does not reach the type, which turns "no request was made"
    into a confusing ``call_args is None``).
    """

    def __init__(self, response):
        self._response = response
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def __call__(self, url, headers=None, **_kwargs):
        self.calls.append((url, headers))
        return self._response

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def url(self) -> str:
        assert self.calls, "no request was issued"
        return self.calls[-1][0]

    @property
    def headers(self) -> dict[str, str] | None:
        assert self.calls, "no request was issued"
        return self.calls[-1][1]


class _FakeAsyncClient:
    """Minimal async context-manager shim around a :class:`_RecordedGet`."""

    def __init__(self, get: _RecordedGet):
        self.get = get

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _stub_http(monkeypatch, response) -> _RecordedGet:
    """Patch the httpx client used by ``fetch_skill_content`` and capture calls."""
    get = _RecordedGet(response)
    monkeypatch.setattr(
        skill_source_mod.httpx, "AsyncClient", lambda **_kw: _FakeAsyncClient(get)
    )
    return get


def _stub_common(monkeypatch, *, integration=None, credential=None):
    """Neutralise the collaborators every skill-write route touches."""
    monkeypatch.setattr(
        skill_source_mod, "_validate_skill_url", AsyncMock(side_effect=lambda url: url)
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "get_user_integration",
        AsyncMock(return_value=integration if integration is not None else _integration()),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "resolve_skill_credential",
        AsyncMock(return_value=credential if credential is not None else _credential()),
    )
    monkeypatch.setattr(agent_skills_mod, "log_audit", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "create_skill_version", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "handle_company_managed_toggle", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "apply_owner_transfer", AsyncMock())


def _db():
    """A stub session that applies the column defaults ``flush()`` would.

    ``AgentSkill.created_at`` / ``enabled`` / ``is_public_catalog`` are
    Python-side column defaults, so without a real flush they stay ``None`` and
    the response serialiser trips over them.
    """
    db = AsyncMock()
    added: list = []
    db.add = MagicMock(side_effect=added.append)
    defaults = {"enabled": True, "is_public_catalog": False, "auth_failure_count": 0}

    async def _flush(*_args, **_kwargs):
        now = datetime.now(UTC)
        for obj in added:
            for field, value in defaults.items():
                if getattr(obj, field, None) is None:
                    setattr(obj, field, value)
            if getattr(obj, "created_at", None) is None:
                obj.created_at = now
            if getattr(obj, "updated_at", None) is None:
                obj.updated_at = now

    db.flush = AsyncMock(side_effect=_flush)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
    return db


# ---------------------------------------------------------------------------
# create_skill — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_with_credential_fetches_through_authenticated_contents_api(
    monkeypatch,
):
    _stub_common(monkeypatch)
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))
    db = _db()

    result = await agent_skills_mod.create_skill(
        body=AgentSkillCreate(
            url=BLOB_URL, source_integration_id=str(INTEGRATION_ID)
        ),
        request=MagicMock(),
        user=_user(),
        db=db,
        _agentic_gate=None,
    )

    assert get.url == (
        "https://api.github.com/repos/sopra/agent-skills/contents/SKILL.md?ref=main"
    )
    assert get.headers["Authorization"] == f"Bearer {TOKEN}"
    # The binding is persisted as a reference; the token never touches the row.
    assert result.source_integration_id == str(INTEGRATION_ID)
    assert result.content == SKILL_BODY
    assert result.name == "triage"


@pytest.mark.asyncio
async def test_create_with_source_ref_tracks_that_branch(monkeypatch):
    _stub_common(monkeypatch)
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    result = await agent_skills_mod.create_skill(
        body=AgentSkillCreate(
            url=BLOB_URL,
            source_integration_id=str(INTEGRATION_ID),
            source_ref="develop",
        ),
        request=MagicMock(),
        user=_user(),
        db=_db(),
        _agentic_gate=None,
    )

    assert get.url.endswith("?ref=develop")
    assert result.source_ref == "develop"


@pytest.mark.asyncio
async def test_create_without_credential_keeps_the_anonymous_fetch(monkeypatch):
    """Omitting the credential must not change anything for public URLs."""
    _stub_common(monkeypatch)
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    result = await agent_skills_mod.create_skill(
        body=AgentSkillCreate(url="https://example.com/SKILL.md"),
        request=MagicMock(),
        user=_user(),
        db=_db(),
        _agentic_gate=None,
    )

    assert get.url == "https://example.com/SKILL.md"
    assert get.headers is None
    assert result.source_integration_id is None
    assert result.auth_failure_count == 0


# ---------------------------------------------------------------------------
# create_skill — rejections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_credential_on_a_non_github_url(monkeypatch):
    """A GitHub token must never be attached to another origin."""
    _stub_common(monkeypatch)
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                url="https://example.com/SKILL.md",
                source_integration_id=str(INTEGRATION_ID),
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 422
    assert "GitHub repository file URL" in exc.value.detail
    assert not get.called


@pytest.mark.asyncio
async def test_create_rejects_credential_without_a_url(monkeypatch):
    _stub_common(monkeypatch)
    _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                content="# pasted", source_integration_id=str(INTEGRATION_ID)
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 422
    assert "URL-based skill" in exc.value.detail


@pytest.mark.asyncio
async def test_create_rejects_a_non_github_integration(monkeypatch):
    _stub_common(monkeypatch, integration=_integration(provider="slack", name="Slack"))
    _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                url=BLOB_URL, source_integration_id=str(INTEGRATION_ID)
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 422
    assert "only authenticate against GitHub" in exc.value.detail


@pytest.mark.asyncio
async def test_create_rejects_a_disconnected_integration(monkeypatch):
    _stub_common(monkeypatch, integration=_integration(status="disconnected"))
    _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                url=BLOB_URL, source_integration_id=str(INTEGRATION_ID)
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 422
    assert "not connected" in exc.value.detail


@pytest.mark.asyncio
async def test_create_surfaces_a_rejected_credential_distinctly(monkeypatch):
    """GitHub 401 at create time names the credential, not the path."""
    _stub_common(monkeypatch)
    _stub_http(monkeypatch, _http_response(401, headers={}))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                url=BLOB_URL, source_integration_id=str(INTEGRATION_ID)
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 422
    assert "rejected the credential" in exc.value.detail
    assert "Sopra GitHub" in exc.value.detail
    assert TOKEN not in exc.value.detail


@pytest.mark.asyncio
async def test_create_explains_the_ambiguous_404(monkeypatch):
    """A 404 on a private repo is path-or-access — say both, not just "missing"."""
    _stub_common(monkeypatch)
    _stub_http(monkeypatch, _http_response(404))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                url=BLOB_URL, source_integration_id=str(INTEGRATION_ID)
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 422
    assert "does not grant access to the repository" in exc.value.detail


@pytest.mark.asyncio
async def test_create_maps_a_rate_limit_to_502(monkeypatch):
    """A throttle is the provider's fault, not the request's — 502, retryable."""
    _stub_common(monkeypatch)
    _stub_http(
        monkeypatch, _http_response(403, headers={"x-ratelimit-remaining": "0"})
    )

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.create_skill(
            body=AgentSkillCreate(
                url=BLOB_URL, source_integration_id=str(INTEGRATION_ID)
            ),
            request=MagicMock(),
            user=_user(),
            db=_db(),
            _agentic_gate=None,
        )

    assert exc.value.status_code == 502
    assert "throttling" in exc.value.detail


# ---------------------------------------------------------------------------
# update_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_attaching_a_credential_refetches_immediately(monkeypatch):
    """Binding a credential re-validates now, without changing the URL."""
    skill = _skill(source_integration_id=None, content="stale")
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(source_integration_id=str(INTEGRATION_ID)),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert get.headers["Authorization"] == f"Bearer {TOKEN}"
    assert skill.source_integration_id == INTEGRATION_ID
    assert skill.content == SKILL_BODY


@pytest.mark.asyncio
async def test_update_changing_the_ref_refetches_that_branch(monkeypatch):
    skill = _skill(content="stale")
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(source_ref="develop"),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert get.url.endswith("?ref=develop")
    assert skill.source_ref == "develop"


@pytest.mark.asyncio
async def test_update_unrelated_field_works_with_a_broken_credential(monkeypatch):
    """A rename must not be blocked by a since-disconnected integration.

    The credential is only resolved when the request actually re-reads the
    source; otherwise a revoked token would lock every unrelated edit to the
    skill, including detaching the credential in the first place.
    """
    skill = _skill(auth_failure_count=3)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "get_user_integration",
        AsyncMock(side_effect=AssertionError("must not resolve the credential")),
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(name="renamed", enabled=False),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert not get.called
    assert skill.name == "renamed"
    assert skill.enabled is False
    # The binding survives an unrelated edit untouched.
    assert skill.source_integration_id == INTEGRATION_ID
    assert skill.auth_failure_count == 3


@pytest.mark.asyncio
async def test_update_rejects_a_credential_on_a_content_only_skill(monkeypatch):
    """There is nothing to authenticate when the skill has no URL."""
    skill = _skill(url=None, source_integration_id=None)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.update_skill(
            skill_id=str(skill.id),
            body=AgentSkillUpdate(source_integration_id=str(INTEGRATION_ID)),
            request=MagicMock(),
            user=_user(is_admin=True),
            db=_db(),
        )

    assert exc.value.status_code == 422
    assert "URL-based skill" in exc.value.detail


@pytest.mark.asyncio
async def test_update_detaching_the_credential_clears_the_auth_state(monkeypatch):
    """Explicit ``null`` detaches the credential and retires its failure state.

    Detaching is a pure configuration change and deliberately does NOT re-read
    the source — see the regression test below for why re-fetching here made
    the credential impossible to remove.
    """
    skill = _skill(auth_failure_count=3, source_ref="develop")
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(source_integration_id=None),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert skill.source_integration_id is None
    assert skill.source_ref is None, "a tracked branch is meaningless without a credential"
    assert skill.auth_failure_count == 0
    assert not get.called, "detaching must not re-read the source"


@pytest.mark.asyncio
async def test_update_can_detach_a_credential_from_an_unreadable_private_url(
    monkeypatch,
):
    """Detaching must succeed even when the URL is unreadable anonymously.

    Regression: re-fetching on detach sent an anonymous GET to a private
    repository, GitHub answered 404, and the route turned that into a 422 — so
    the credential could never be removed and the skill was stuck.
    """
    skill = _skill(auth_failure_count=2)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    # What GitHub really answers an anonymous read of a private file.
    get = _stub_http(monkeypatch, _http_response(404))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(source_integration_id=None),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert skill.source_integration_id is None
    assert skill.auth_failure_count == 0
    assert not get.called
    # The previously-fetched body survives; the next scheduled refresh then
    # fails on its own merits and follows the normal quarantine path.
    assert skill.content == "old body"


@pytest.mark.asyncio
async def test_update_detaching_while_changing_the_url_refetches_anonymously(
    monkeypatch,
):
    """A detach combined with a new URL still has to read that new URL."""
    skill = _skill(auth_failure_count=1)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(
            url="https://example.com/public/SKILL.md", source_integration_id=None
        ),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert get.url == "https://example.com/public/SKILL.md"
    assert get.headers is None, "the new URL must be read anonymously"
    assert skill.source_integration_id is None
    assert skill.content == SKILL_BODY


@pytest.mark.asyncio
async def test_update_unrelated_field_does_not_refetch(monkeypatch):
    """Renaming a skill must not trigger a network round-trip."""
    skill = _skill()
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(name="renamed"),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert not get.called
    assert skill.name == "renamed"
    assert skill.source_integration_id == INTEGRATION_ID


@pytest.mark.asyncio
async def test_update_replacing_content_drops_the_source_binding(monkeypatch):
    """A pasted body has no remote source, so the credential goes with the URL."""
    skill = _skill(auth_failure_count=2)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.update_skill(
        skill_id=str(skill.id),
        body=AgentSkillUpdate(content="# pasted instead"),
        request=MagicMock(),
        user=_user(is_admin=True),
        db=_db(),
    )

    assert skill.url is None
    assert skill.source_integration_id is None
    assert skill.source_ref is None
    assert skill.auth_failure_count == 0
    assert skill.refresh_failure_count == 0


# ---------------------------------------------------------------------------
# refresh_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_refresh_uses_the_bound_credential(monkeypatch):
    """The Refresh button must work for a private repo, not only the scheduler."""
    skill = _skill(content="stale", refresh_failure_count=1, auth_failure_count=2)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=skill)
    )
    get = _stub_http(monkeypatch, _http_response(200, text=SKILL_BODY))

    await agent_skills_mod.refresh_skill(
        skill_id=str(skill.id), user=_user(is_admin=True), db=_db()
    )

    assert get.headers["Authorization"] == f"Bearer {TOKEN}"
    assert skill.content == SKILL_BODY
    assert skill.refresh_failure_count == 0
    assert skill.auth_failure_count == 0
