"""Tests for Phase-3 integration tool-build performance work.

Covers:

* P3.1 — expiring OAuth tokens are refreshed CONCURRENTLY by
  ``_parallel_refresh_expiring_tokens`` (previously: sequential inline
  refreshes inside ``_get_token``, N x up to 30 s in the setup phase), and
  a failed parallel attempt is never retried sequentially.
* P3.2 — ``_get_token`` reuses a batch-loaded Integration row instead of
  re-querying the DB per binding.
* P3.5 — ``load_custom_and_mcp_tools`` overlaps the two loaders on separate
  sessions while replaying registrations in a deterministic order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.builtin_tools.loaders as loaders
import app.services.integration_tools as integration_tools
from tests.rendezvous import Rendezvous

_TOKEN = "fresh-access-token"
# The integration's owner: an OAuth token only resolves for them.
_OWNER_ID = uuid.uuid4()


class _FakeIntegration:
    def __init__(
        self,
        *,
        provider: str = "slack",
        auth_type: str = "oauth",
        expires_in_minutes: int | None = 1,
        has_refresh_token: bool = True,
    ):
        self.id = uuid.uuid4()
        self.name = f"{provider}-integration"
        self.provider = provider
        self.auth_type = auth_type
        self.access_token_enc = b"encrypted-access"
        self.api_key_enc = None
        self.extra_config_enc = None
        self.refresh_token_enc = b"encrypted-refresh" if has_refresh_token else None
        self.token_expires_at = (
            datetime.now(UTC) + timedelta(minutes=expires_in_minutes)
            if expires_in_minutes is not None
            else None
        )
        self.token_meta = None
        self.installation_id = None
        self.is_company_managed = False
        self.user_id = _OWNER_ID
        self.scope_kind = None
        self.scope_id = None
        self.binding_metadata = None
        self.client_id_enc = b"encrypted-client-id"
        self.client_secret_enc = None
        self.token_url = "https://example.com/token"
        self.scopes = None
        self.status = "connected"


class _ExplodingDB:
    """A DB stub that fails loudly if any query is issued."""

    async def execute(self, _statement):
        raise AssertionError(
            "_get_token must not query the DB when a batch-loaded row is passed"
        )


# ---------------------------------------------------------------------------
# P3.1 — parallel refresh pass
# ---------------------------------------------------------------------------


def test_needs_proactive_refresh_predicate():
    """The predicate mirrors the inline-refresh decision in ``_get_token``."""
    # Expiring soon + refresh token -> refresh.
    assert integration_tools._needs_proactive_refresh(
        _FakeIntegration(expires_in_minutes=1)
    )
    # No expiry tracked: scheduler stamps a synthetic TTL; chat must not
    # treat NULL as "refresh now" (Notion omits expires_in).
    assert not integration_tools._needs_proactive_refresh(
        _FakeIntegration(expires_in_minutes=None)
    )
    # Fresh token -> no refresh.
    assert not integration_tools._needs_proactive_refresh(
        _FakeIntegration(expires_in_minutes=120)
    )
    # No refresh token -> the legacy warn-and-continue path in _get_token.
    assert not integration_tools._needs_proactive_refresh(
        _FakeIntegration(expires_in_minutes=1, has_refresh_token=False)
    )
    # Non-OAuth rows are never proactively refreshed.
    assert not integration_tools._needs_proactive_refresh(
        _FakeIntegration(auth_type="api_key")
    )


@pytest.mark.asyncio
async def test_parallel_refresh_runs_concurrently(monkeypatch):
    """Three expiring tokens must refresh concurrently, not sequentially."""
    rows = [_FakeIntegration(expires_in_minutes=1) for _ in range(3)]
    rendezvous = Rendezvous(len(rows))

    async def _fake_refresh(_db, integration_id: str) -> str:
        await rendezvous.arrive(integration_id)
        return f"token-{integration_id}"

    monkeypatch.setattr(integration_tools, "_refresh_by_id", _fake_refresh)

    prefetched, attempted = (
        await integration_tools._parallel_refresh_expiring_tokens(None, rows)
    )

    assert rendezvous.missed == [], "refreshes must overlap"
    assert len(prefetched) == 3
    assert attempted == {str(r.id) for r in rows}


@pytest.mark.asyncio
async def test_parallel_refresh_failure_marks_attempted(monkeypatch):
    """A failed refresh lands in ``attempted`` (never retried sequentially)
    but not in ``prefetched``."""
    ok_row = _FakeIntegration(expires_in_minutes=1)
    bad_row = _FakeIntegration(expires_in_minutes=1)

    async def _fake_refresh(_db, integration_id: str) -> str:
        if integration_id == str(bad_row.id):
            raise RuntimeError("token endpoint down")
        return _TOKEN

    monkeypatch.setattr(integration_tools, "_refresh_by_id", _fake_refresh)

    prefetched, attempted = (
        await integration_tools._parallel_refresh_expiring_tokens(
            None, [ok_row, bad_row]
        )
    )
    assert prefetched == {str(ok_row.id): _TOKEN}
    assert attempted == {str(ok_row.id), str(bad_row.id)}


# ---------------------------------------------------------------------------
# P3.2 — _get_token row reuse + prefetched-token consumption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_token_uses_batch_loaded_row_and_prefetched_token(monkeypatch):
    """With ``integ=`` and ``prefetched_token=`` supplied, ``_get_token``
    must issue NO DB query and NO sequential inline refresh."""

    async def _must_not_refresh(*_a, **_k):
        raise AssertionError("inline refresh must not run for prefetched tokens")

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _must_not_refresh)
    monkeypatch.setattr(
        integration_tools, "decrypt_value", lambda _blob: "stale-stored-token"
    )

    integ = _FakeIntegration(expires_in_minutes=1)
    token = await integration_tools._get_token(
        _ExplodingDB(),  # type: ignore[arg-type]
        str(integ.id),
        provider="slack",
        user_id=str(_OWNER_ID),
        integ=integ,
        prefetched_token=_TOKEN,
    )
    assert token == _TOKEN


@pytest.mark.asyncio
async def test_get_token_failed_parallel_refresh_uses_stored_token(monkeypatch):
    """``refresh_attempted=True`` without a prefetched token must surface the
    (possibly expired) stored token — the legacy failed-refresh behaviour —
    WITHOUT retrying the refresh sequentially."""

    async def _must_not_refresh(*_a, **_k):
        raise AssertionError("inline refresh must not be retried")

    monkeypatch.setattr(integration_tools, "_try_inline_refresh", _must_not_refresh)
    monkeypatch.setattr(
        integration_tools, "decrypt_value", lambda _blob: "stale-stored-token"
    )

    integ = _FakeIntegration(expires_in_minutes=1)
    token = await integration_tools._get_token(
        _ExplodingDB(),  # type: ignore[arg-type]
        str(integ.id),
        provider="slack",
        user_id=str(_OWNER_ID),
        integ=integ,
        refresh_attempted=True,
    )
    assert token == "stale-stored-token"


# ---------------------------------------------------------------------------
# P3.5 — split-session parallel loader
# ---------------------------------------------------------------------------


class _RecordingComposite:
    def __init__(self) -> None:
        self.batches: list[str] = []

    def add_tools(self, definitions, _executor) -> None:
        self.batches.extend(d for d in definitions)


@pytest.mark.asyncio
async def test_load_custom_and_mcp_tools_overlaps_and_orders(monkeypatch):
    """Both loaders run concurrently (each on its own session) and the
    registrations are replayed in the caller's declared order."""
    timeline: list[str] = []
    rendezvous = Rendezvous(2)

    async def _fake_custom(db, ids, composite, **_kwargs):
        timeline.append("custom-start")
        await rendezvous.arrive("custom")
        composite.add_tools(["custom_tool"], object())
        timeline.append("custom-end")

    async def _fake_mcp(
        db, ids, composite, allowed_tools=None, artifact_scope=None, acting_user_id=None
    ):
        timeline.append("mcp-start")
        await rendezvous.arrive("mcp")
        composite.add_tools(["mcp_tool"], object())
        timeline.append("mcp-end")
        return SimpleNamespace(kind="manager")

    class _FakeSessionCtx:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(loaders, "load_custom_tools", _fake_custom)
    monkeypatch.setattr(loaders, "load_mcp_tools", _fake_mcp)
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _FakeSessionCtx()
    )

    composite = _RecordingComposite()
    manager = await loaders.load_custom_and_mcp_tools(
        SimpleNamespace(),
        custom_tool_ids=["ct-1"],
        mcp_ids=["mcp-1"],
        composite=composite,
        mcp_first=False,
    )

    assert getattr(manager, "kind", None) == "manager"
    # Overlap: both loaders started before either finished.
    assert rendezvous.missed == [], "loaders should overlap"
    assert timeline.index("mcp-start") < timeline.index("custom-end")
    # Deterministic replay order: custom first (agent_chat legacy order).
    assert composite.batches == ["custom_tool", "mcp_tool"]

    # A fresh rendezvous: the first one is open and would prove nothing.
    rendezvous = Rendezvous(2)
    composite2 = _RecordingComposite()
    await loaders.load_custom_and_mcp_tools(
        SimpleNamespace(),
        custom_tool_ids=["ct-1"],
        mcp_ids=["mcp-1"],
        composite=composite2,
        mcp_first=True,
    )
    assert rendezvous.missed == [], "loaders should overlap"
    assert composite2.batches == ["mcp_tool", "custom_tool"]


@pytest.mark.asyncio
async def test_load_custom_and_mcp_tools_single_loader_fast_paths(monkeypatch):
    """With only one kind of work the helper delegates directly (no second
    session, exactly the legacy path)."""
    calls: list[str] = []

    async def _fake_custom(db, ids, composite, **_kwargs):
        calls.append("custom")

    async def _fake_mcp(
        db, ids, composite, allowed_tools=None, artifact_scope=None, acting_user_id=None
    ):
        calls.append("mcp")
        return "manager"

    monkeypatch.setattr(loaders, "load_custom_tools", _fake_custom)
    monkeypatch.setattr(loaders, "load_mcp_tools", _fake_mcp)

    composite = _RecordingComposite()
    assert (
        await loaders.load_custom_and_mcp_tools(
            SimpleNamespace(),
            custom_tool_ids=["ct-1"],
            mcp_ids=[],
            composite=composite,
        )
        is None
    )
    assert (
        await loaders.load_custom_and_mcp_tools(
            SimpleNamespace(),
            custom_tool_ids=[],
            mcp_ids=["mcp-1"],
            composite=composite,
        )
        == "manager"
    )
    assert (
        await loaders.load_custom_and_mcp_tools(
            SimpleNamespace(),
            custom_tool_ids=[],
            mcp_ids=[],
            composite=composite,
        )
        is None
    )
    assert calls == ["custom", "mcp"]
