"""Regression coverage for company-managed shared-visibility paths.

Company-managed runs always carry an owner / run-as
``user_id`` (previously ``NULL``). Two downstream paths used the
``user_id``-absent proxy to decide "is this shared?" and silently
regressed once an owner was always stamped:

1. ``_publish_run_status`` (SSE status fan-out) — must broadcast a
   company-managed run's live status to the shared ``activity:company``
   channel so *every* connected user keeps seeing it, not just the
   owner / run-as user. Driven by the explicit ``is_company`` flag now,
   not by ``user_id`` being unset.

2. ``learn_email_style`` (per-user style learning) — must stamp the
   embedding with the *requesting* user's id even for a company-managed
   agent. Personal writing style is learned from one user's sent emails;
   storing it shared (``user_id`` NULL) would let hybrid search surface
   one user's style to every other user of the same shared agent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assignment_helpers, learning, notify


def _notify_run(*, is_company_managed, user_id="owner-id"):
    return SimpleNamespace(
        id="run-1",
        status="completed",
        error=None,
        result_summary="Digest ready",
        is_company_managed=is_company_managed,
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_company_run_broadcasts_once_even_via_external_channel(monkeypatch):
    """A company-managed run delivered via a successful external channel
    (teams) must STILL broadcast to ``activity:company`` exactly once — the
    external delivery path never calls ``_notify_web`` on success, so the
    top-level broadcast in ``deliver_notification`` is what guarantees
    connected users see the shared run notification."""
    monkeypatch.setattr(notify, "_persist_notification_activity", AsyncMock())
    monkeypatch.setattr(notify, "_notify_teams", AsyncMock())  # external delivery OK
    web = AsyncMock()
    monkeypatch.setattr(notify, "_notify_web", web)

    run = _notify_run(is_company_managed=True)
    assignment = SimpleNamespace(
        name="Nightly digest", trigger_config={"notify_via": "teams"}
    )
    agent = SimpleNamespace(name="CTEM Assistant")

    await notify.deliver_notification(AsyncMock(), run, assignment, agent)

    web.assert_awaited_once()
    assert web.await_args.kwargs["is_company"] is True


@pytest.mark.asyncio
async def test_company_run_web_default_broadcasts_exactly_once(monkeypatch):
    """The web-default path must not double-publish for company-managed runs:
    the top-level broadcast fires once and the web-default branch is skipped."""
    monkeypatch.setattr(notify, "_persist_notification_activity", AsyncMock())
    web = AsyncMock()
    monkeypatch.setattr(notify, "_notify_web", web)

    run = _notify_run(is_company_managed=True)
    assignment = SimpleNamespace(name="Digest", trigger_config={})  # notify_via=web
    agent = SimpleNamespace(name="Agent")

    await notify.deliver_notification(AsyncMock(), run, assignment, agent)

    web.assert_awaited_once()
    assert web.await_args.kwargs["is_company"] is True


@pytest.mark.asyncio
async def test_personal_run_via_external_channel_no_company_broadcast(monkeypatch):
    """A personal run delivered via a (successful) external channel triggers
    no top-level company broadcast — only the external delivery runs."""
    monkeypatch.setattr(notify, "_persist_notification_activity", AsyncMock())
    monkeypatch.setattr(notify, "_notify_teams", AsyncMock())
    web = AsyncMock()
    monkeypatch.setattr(notify, "_notify_web", web)

    run = _notify_run(is_company_managed=False)
    assignment = SimpleNamespace(
        name="Digest", trigger_config={"notify_via": "teams"}
    )
    agent = SimpleNamespace(name="Agent")

    await notify.deliver_notification(AsyncMock(), run, assignment, agent)

    web.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_run_status_company_broadcasts_to_company_channel(monkeypatch):
    """A company-managed run (is_company=True) must publish to
    ``activity:company`` even though it now carries an owner ``user_id``."""
    redis = AsyncMock()
    monkeypatch.setattr(assignment_helpers, "redis_client", AsyncMock(return_value=redis))

    await assignment_helpers._publish_run_status(
        "owner-user-id",
        "run-1",
        "running",
        "Nightly digest",
        is_company=True,
    )

    redis.publish.assert_awaited_once()
    channel = redis.publish.await_args.args[0]
    assert channel == "activity:company"


@pytest.mark.asyncio
async def test_publish_run_status_personal_goes_to_owner_channel(monkeypatch):
    """A personal run (is_company=False) publishes only to the owner's channel."""
    redis = AsyncMock()
    monkeypatch.setattr(assignment_helpers, "redis_client", AsyncMock(return_value=redis))

    await assignment_helpers._publish_run_status(
        "owner-user-id",
        "run-1",
        "running",
        "Nightly digest",
        is_company=False,
    )

    redis.publish.assert_awaited_once()
    channel = redis.publish.await_args.args[0]
    assert channel == "activity:owner-user-id"


@pytest.mark.asyncio
async def test_learn_email_style_stamps_requesting_user_for_company_agent(monkeypatch):
    """Style-guide embeddings must be owned by the requesting user, never
    stamped as shared (``user_id`` NULL), so one user's learned writing
    style can't be retrieved by other users of a company-managed agent."""
    stored = AsyncMock()
    monkeypatch.setattr(learning, "store_embedding", stored)

    # A resolver that returns NULL (shared) would be the regression — assert
    # it is NOT consulted on the style path.
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(learning, "_resolve_embedding_owner", resolver)

    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="Style guide")))
    embedder = SimpleNamespace(
        embed=AsyncMock(return_value=[0.1, 0.2, 0.3]),
        provider_name="openai",
    )

    await learning.learn_email_style(
        db=AsyncMock(),
        llm=llm,
        embedding_provider=embedder,
        user_id="requesting-user-id",
        agent_id="company-managed-agent-id",
        emails=[{"to": "a@b.c", "subject": "Hi", "body": "Hello there"}],
        embedding_model="text-embedding-3-small",
    )

    stored.assert_awaited_once()
    assert stored.await_args.kwargs["user_id"] == "requesting-user-id"
    assert stored.await_args.kwargs["source_type"] == "style_guide"
    # The shared-owner resolver must not gate personal style embeddings.
    resolver.assert_not_awaited()
