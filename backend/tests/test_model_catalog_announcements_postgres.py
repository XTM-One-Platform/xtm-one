"""Model catalog announcements against a real database.

Recording at the env seed (what the latest row says, a crash between the seed
and the record, folding a change nobody was emailed about) and delivering from
the scheduler (who is told, batches, order, refused recipients, back-off,
leases, a dead or late worker never emailing anyone twice, expiry).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.core.config import settings
from app.core.security import encrypt_value
from app.llm.router import VIDEO_DEFAULTS
from app.models.llm_config import LLMProviderConfig
from app.models.model_catalog_announcement import ModelCatalogAnnouncement, ModelCatalogAnnouncementRecipient
from app.models.user import User
from app.services import model_catalog_announcements as mca
from app.services.email import EmailDeliveryError, reply_names_the_address
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

ADMIN_EMAIL = "admin@acme.one.test"
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "default_model", "claude-sonnet-5")
    monkeypatch.setattr(settings, "admin_email", ADMIN_EMAIL)
    monkeypatch.setattr(settings, "model_announcement_emails_disabled", False)
    monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
    monkeypatch.setattr(settings, "model_catalog_emails_recipients", "all")
    monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 20)
    # Delivered from the next tick; the settle hold has its own tests.
    monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 0)
    monkeypatch.setattr(settings, "smtp_hostname", "smtp.acme.test")
    monkeypatch.setattr(settings, "frontend_url", "https://acme.one.test")


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("mcatann", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed(sessions, models: list[str], default: str | None, *, provider: str = "anthropic") -> None:
    """What the env seed leaves: an env-seeded provider row and the platform default."""
    async with sessions() as db:
        row = (
            await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == provider))
        ).scalar_one_or_none()
        if row is None:
            row = LLMProviderConfig(
                provider=provider, api_key_enc=encrypt_value("sk-test"), is_from_env=True, user_id=None
            )
            db.add(row)
        row.available_models = list(models)
        row.env_models = list(models)
        row.default_model = models[0] if models else None
        ps = await get_platform_settings(db)
        ps.agent_default_model = default
        ps.agent_default_provider = provider if default else None
        await db.commit()


async def _catalog(sessions) -> mca.ModelCatalog:
    async with sessions() as db:
        return await mca.read_model_catalog(db)


async def _boot(sessions, models: list[str], default: str | None, *, now: datetime = NOW):
    """One boot: read, seed, read, record - what ``model_catalog_watch`` does."""
    before = await _catalog(sessions)
    await _seed(sessions, models, default)
    after = await _catalog(sessions)
    async with sessions() as db:
        return await mca.record_catalog_change(db, before, after, now=now)


async def _apply_boot(sessions, *, now: datetime) -> None:
    """The end of a boot: settle the queued announcements for its configuration."""
    async with sessions() as db:
        await mca.apply_boot_configuration(db, await mca.read_model_catalog(db), now=now)


async def _rows(sessions) -> list[ModelCatalogAnnouncement]:
    async with sessions() as db:
        return list(
            (await db.execute(select(ModelCatalogAnnouncement).order_by(ModelCatalogAnnouncement.created_at))).scalars()
        )


async def _users(sessions, *users: tuple[str, str, bool]) -> dict[str, str]:
    ids = {}
    async with sessions() as db:
        for email, role, active in users:
            user = User(email=email, full_name="Josh Smith", role=role, is_active=active)
            db.add(user)
            await db.flush()
            ids[email] = str(user.id)
        await db.commit()
    return ids


async def _outcomes(sessions, announcement_id) -> dict[str, str]:
    """Recipient user id -> what the send did."""
    async with sessions() as db:
        rows = await db.execute(
            select(ModelCatalogAnnouncementRecipient.user_id, ModelCatalogAnnouncementRecipient.status).where(
                ModelCatalogAnnouncementRecipient.announcement_id == announcement_id
            )
        )
        return {str(user_id): status for user_id, status in rows.all()}


class _Outbox:
    def __init__(self, fail: dict[str, Exception] | None = None, on_send=None) -> None:
        self.sent: list[dict] = []
        self.fail = dict(fail or {})
        self.on_send = on_send

    async def __call__(self, **kwargs) -> None:
        error = self.fail.get(kwargs["to"])
        if error is not None:
            raise error
        self.sent.append(kwargs)
        if self.on_send is not None:
            await self.on_send(kwargs["to"])

    @property
    def recipients(self) -> list[str]:
        return [message["to"] for message in self.sent]


SONNET_5 = ["claude-sonnet-5", "claude-haiku-4-5"]
SONNET_55 = ["claude-sonnet-5-5", "claude-haiku-4-5"]


# --- Recording ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_boot_records_the_baseline_and_the_rollout_it_brings(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")

        row = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")

        assert row is not None and row.status == "pending"
        rows = await _rows(sessions)
        assert [r.status for r in rows] == ["baseline", "pending"]
        change = mca.diff_catalogs(mca._catalog_before(rows[1]), mca._catalog_after(rows[1]))
        assert change.new_default == mca.CatalogModel("claude-sonnet-5-5", "anthropic")
        assert [m.id for m in change.removed] == ["claude-sonnet-5"]

        # The next replica boots on the seeded catalog: nothing new to say.
        assert await _boot(sessions, SONNET_55, "claude-sonnet-5-5", now=NOW + timedelta(minutes=1)) is None
        assert len(await _rows(sessions)) == 2


@pytest.mark.asyncio
async def test_a_change_the_previous_boot_seeded_without_recording_it_is_caught(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        assert await _boot(sessions, SONNET_5, "claude-sonnet-5") is None  # records the baseline
        assert [r.status for r in await _rows(sessions)] == ["baseline"]

        # A boot seeds Sonnet 5.5 and dies before recording it.
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")

        # The next boot sees no change live, but the latest row still knows Sonnet 5.
        row = await _boot(sessions, SONNET_55, "claude-sonnet-5-5", now=NOW + timedelta(minutes=5))
        assert row is not None and row.status == "pending"
        assert row.previous_default_model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_the_watch_records_the_seed_even_when_the_catalog_could_not_be_read_before(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        # The watch records on the real clock: the baseline comes before it.
        earlier = datetime.now(UTC) - timedelta(hours=1)
        assert await _boot(sessions, SONNET_5, "claude-sonnet-5", now=earlier) is None

        real_read = mca.read_model_catalog
        calls = 0

        async def flaky_read(db):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("pool exhausted")
            return await real_read(db)

        monkeypatch.setattr(mca, "read_model_catalog", flaky_read)
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
        async with mca.model_catalog_watch():
            await _seed(sessions, SONNET_55, "claude-sonnet-5-5")

        row = (await _rows(sessions))[-1]
        assert (row.status, row.previous_default_model, row.default_model) == (
            "pending",
            "claude-sonnet-5",
            "claude-sonnet-5-5",
        )


def _prod_env(monkeypatch, anthropic_models: str) -> None:
    """The SaaS pod's environment: pinned env catalogs, ``DEFAULT_MODEL`` left behind."""
    import os

    import app.main as app_main

    for key in [k for k in os.environ if k.startswith("LLM_PROVIDER_")]:
        monkeypatch.delenv(key)
    monkeypatch.setattr(app_main, "_load_dotenv_files", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_PROVIDER_ANTHROPIC_MODELS", anthropic_models)
    monkeypatch.setenv("LLM_PROVIDER_OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("LLM_PROVIDER_OPENAI_MODELS", "gpt-image-2")
    monkeypatch.setattr(settings, "default_routing_model", "claude-haiku-4-5")
    monkeypatch.setattr(settings, "llm_provider_models_editable", False)


async def _env_boot() -> None:
    """What ``provision_env_providers`` runs at boot: the real seed, watched."""
    from app.main import _seed_llm_providers_from_env

    async with mca.model_catalog_watch():
        await _seed_llm_providers_from_env()


@pytest.mark.asyncio
async def test_the_real_env_seed_announces_a_rollout_that_left_default_model_behind(monkeypatch):
    # The SaaS rollout: LLM_PROVIDER_ANTHROPIC_MODELS moves from Sonnet 5 to
    # Sonnet 5.5 while DEFAULT_MODEL still names Sonnet 5, which no provider
    # offers any more.
    async with _database() as sessions:
        _prod_env(monkeypatch, "claude-sonnet-5,claude-haiku-4-5")
        await _env_boot()
        assert (await _catalog(sessions)).default_model == "claude-sonnet-5"

        _prod_env(monkeypatch, "claude-sonnet-5-5,claude-haiku-4-5")
        await _env_boot()

        after = await _catalog(sessions)
        assert [m.id for m in after.models] == ["claude-haiku-4-5", "claude-sonnet-5-5"]
        # The seed falls back to the provider's first model.
        assert (after.default_model, after.default_provider) == ("claude-sonnet-5-5", "anthropic")
        row = (await _rows(sessions))[-1]
        assert row.status == "pending", (row.status, row.skip_reason, row.default_model, row.models)
        change = mca.announcement_change(row)
        assert [m.id for m in change.removed] == ["claude-sonnet-5"]
        assert change.announces


@pytest.mark.asyncio
async def test_a_fresh_install_announces_nothing():
    async with _database() as sessions:
        row = await _boot(sessions, SONNET_5, "claude-sonnet-5")
        assert row is not None and row.status == "recorded"
        assert [r.status for r in await _rows(sessions)] == ["baseline", "recorded"]


@pytest.mark.asyncio
async def test_an_undelivered_change_is_folded_and_a_reverted_one_is_never_sent(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
        first = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")

        # Opus 5.5 lands before anyone was emailed: still one announcement.
        folded = await _boot(
            sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5", now=NOW + timedelta(minutes=1)
        )
        assert folded is not None and folded.id == first.id and folded.status == "pending"
        change = mca.diff_catalogs(mca._catalog_before(folded), mca._catalog_after(folded))
        assert change.new_default.id == "claude-sonnet-5-5"
        assert [m.id for m in change.added] == ["claude-opus-5-5"]

        # Then everything is rolled back: nothing left to say.
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5")
        reverted = await _boot(sessions, SONNET_5, "claude-sonnet-5", now=NOW + timedelta(minutes=2))
        assert reverted.id == first.id and reverted.status == "superseded"
        assert [r.status for r in await _rows(sessions)] == ["baseline", "superseded"]


async def _skipped_rollout(sessions, monkeypatch) -> ModelCatalogAnnouncement:
    """The Sonnet 5 -> 5.5 rollout, recorded by a boot with the emails off."""
    await _seed(sessions, SONNET_5, "claude-sonnet-5")
    monkeypatch.setattr(settings, "model_catalog_emails_enabled", False)
    monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
    row = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")
    assert (row.status, row.skip_reason) == ("skipped", "disabled")
    return row


@pytest.mark.asyncio
async def test_a_change_skipped_for_the_configuration_is_sent_once_a_boot_can_send_it(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        skipped = await _skipped_rollout(sessions, monkeypatch)

        # Delivery never decides on its own settings: turning the switch on in a
        # running process sends nothing...
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0

        # ...the next boot, running with it, does.
        await _apply_boot(sessions, now=NOW + timedelta(hours=1))
        row = (await _rows(sessions))[-1]
        assert (row.id, row.status, row.skip_reason) == (skipped.id, "pending", None)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(hours=1)) == 1
        assert outbox.recipients == ["ann@acme.test"]
        assert "Claude Sonnet 5.5" in outbox.sent[0]["subject"]


@pytest.mark.asyncio
async def test_a_skipped_change_is_only_sent_while_it_is_still_the_news(monkeypatch):
    # Too old.
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _skipped_rollout(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        await _apply_boot(sessions, now=NOW + mca.MAX_AGE + timedelta(minutes=1))
        assert (await _rows(sessions))[-1].status == "skipped"

    # An admin emailed the users about the default since.
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _skipped_rollout(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        async with sessions() as db:
            await mca.announce_current_default_model(db, now=NOW + timedelta(minutes=5))
            await db.commit()
        await _apply_boot(sessions, now=NOW + timedelta(hours=1))
        assert [(r.source, r.status) for r in await _rows(sessions)] == [
            ("env", "baseline"),
            ("env", "skipped"),
            ("admin", "pending"),
        ]

    # The env changed the catalog again: the next rollout says it all.
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _skipped_rollout(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        await _seed(sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5")
        await _apply_boot(sessions, now=NOW + timedelta(hours=1))
        assert (await _rows(sessions))[-1].status == "skipped"


@pytest.mark.asyncio
async def test_a_newer_change_folds_into_the_one_nobody_was_told_about(monkeypatch):
    async with _database() as sessions:
        skipped = await _skipped_rollout(sessions, monkeypatch)

        # Opus 5.5 lands at the next rollout, the emails now on: one email says both.
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        folded = await _boot(
            sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5", now=NOW + timedelta(hours=1)
        )
        assert (folded.id, folded.status, folded.skip_reason) == (skipped.id, "pending", None)
        change = mca.announcement_change(folded)
        assert change.new_default == mca.CatalogModel("claude-sonnet-5-5", "anthropic")
        assert change.previous_default == "claude-sonnet-5"
        assert [m.id for m in change.added] == ["claude-opus-5-5"]
        assert [m.id for m in change.removed] == ["claude-sonnet-5"]
        assert [r.status for r in await _rows(sessions)] == ["baseline", "pending"]


# --- Settling: the changes of successive deploys share one email ------------------


@pytest.mark.asyncio
async def test_successive_env_deploys_share_one_email_sent_once_the_catalog_settles(monkeypatch):
    monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 30)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
        first = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")
        assert first.status == "pending"
        assert first.next_attempt_at == first.created_at + timedelta(minutes=30)
        progress = await _progress(sessions)
        assert (progress.held_until, progress.retry_at, progress.remaining) == (first.next_attempt_at, None, 1)

        # Nothing goes out while the catalog may still move...
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=1)) == 0

        # ...and the next deploy joins the same email and restarts the wait.
        later = NOW + timedelta(minutes=20)
        folded = await _boot(sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5", now=later)
        assert (folded.id, folded.status) == (first.id, "pending")
        assert folded.next_attempt_at == later + timedelta(minutes=30)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=35)) == 0
        assert outbox.sent == []

        assert await mca.deliver_pending_announcements(send=outbox, now=folded.next_attempt_at) == 1
        assert outbox.recipients == ["ann@acme.test"]
        # Both deploys in one email: the new default and the model added after it.
        assert outbox.sent[0]["subject"] == f"New models available in your {mca_platform_name()} platform"
        assert "Claude Sonnet 5.5" in outbox.sent[0]["text"]
        assert "Claude Opus 5.5" in outbox.sent[0]["text"]
        assert [r.status for r in await _rows(sessions)] == ["baseline", "sent"]
        assert (await _progress(sessions)).held_until is None


@pytest.mark.asyncio
async def test_a_change_reverted_while_it_settles_is_never_sent(monkeypatch):
    monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 30)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
        first = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")

        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5")
        reverted = await _boot(sessions, SONNET_5, "claude-sonnet-5", now=NOW + timedelta(minutes=10))
        assert (reverted.id, reverted.status, reverted.next_attempt_at) == (first.id, "superseded", None)
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(hours=1)) == 0
        assert outbox.sent == []


@pytest.mark.asyncio
async def test_a_skipped_change_queued_again_at_a_boot_waits_for_the_catalog_to_settle(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        skipped = await _skipped_rollout(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 30)

        boot = NOW + timedelta(hours=1)
        await _apply_boot(sessions, now=boot)
        row = (await _rows(sessions))[-1]
        assert (row.id, row.status, row.next_attempt_at) == (skipped.id, "pending", boot + timedelta(minutes=30))
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=boot + timedelta(minutes=1)) == 0
        assert await mca.deliver_pending_announcements(send=outbox, now=boot + timedelta(minutes=30)) == 1
        assert outbox.recipients == ["ann@acme.test"]


@pytest.mark.asyncio
async def test_an_admin_announcement_is_never_held(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _skipped_rollout(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_settle_minutes", 30)

        async with sessions() as db:
            row = await mca.announce_current_default_model(db, now=NOW + timedelta(minutes=5))
            await db.commit()
        assert (row.source, row.next_attempt_at) == (mca.SOURCE_ADMIN, None)
        assert (await _progress(sessions)).held_until is None
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=5)) == 1
        assert outbox.recipients == ["ann@acme.test"]


@pytest.mark.asyncio
async def test_the_catalog_is_what_users_pick_from(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, ["gpt-image-2", "gpt-5.1"], None, provider="openai")
        await _seed(sessions, ["claude-sonnet-5", "claude-haiku-4-5"], "claude-sonnet-5")
        async with sessions() as db:
            # A provider an admin configured in the UI is not the env's catalog.
            db.add(
                LLMProviderConfig(
                    provider="mistral",
                    api_key_enc=encrypt_value("k"),
                    available_models=["mistral-large-latest"],
                    is_from_env=False,
                )
            )
            # An env-seeded model an admin took out of the catalog is not offered.
            anthropic = (
                await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == "anthropic"))
            ).scalar_one()
            anthropic.available_models = ["claude-sonnet-5"]
            await db.commit()

        catalog = await _catalog(sessions)
        assert [m.id for m in catalog.models] == ["claude-sonnet-5", "gpt-5.1"]  # no image model
        assert catalog.default_model == "claude-sonnet-5"

        # Without DEFAULT_MODEL the default is the admin's, not the env's.
        monkeypatch.setattr(settings, "default_model", "")
        assert (await _catalog(sessions)).default_model is None


# --- Delivering ---------------------------------------------------------------------


async def _pending(sessions, monkeypatch) -> None:
    await _seed(sessions, SONNET_5, "claude-sonnet-5")
    monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
    row = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_active_users_are_emailed_in_batches_never_twice_and_never_the_bootstrap_admin(monkeypatch):
    async with _database() as sessions:
        await _users(
            sessions,
            (ADMIN_EMAIL, "admin", True),
            ("ann@acme.test", "admin", True),
            ("bob@acme.test", "user", True),
            ("cid@acme.test", "user", True),
            ("dan@acme.test", "user", False),
        )
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 2)
        outbox = _Outbox()

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 2
        assert (await _rows(sessions))[-1].status == "pending"
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=1)) == 1
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=2)) == 0

        assert sorted(outbox.recipients) == ["ann@acme.test", "bob@acme.test", "cid@acme.test"]
        row = (await _rows(sessions))[-1]
        assert row.status == "sent" and row.sent_at is not None and row.claim_token is None
        assert sorted((await _outcomes(sessions, row.id)).values()) == ["delivered"] * 3
        message = outbox.sent[0]
        assert message["subject"] == "Claude Sonnet 5.5 is now your default model on " + mca_platform_name()
        # The RFC 3834 headers are the email layer's default, not the caller's.
        assert "headers" not in message
        assert "Hello Josh," in message["text"]


def mca_platform_name() -> str:
    from app.services.branding import get_cached_branding

    return get_cached_branding().name


@pytest.mark.asyncio
async def test_admins_only(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_recipients", "admins")
        outbox = _Outbox()
        await mca.deliver_pending_announcements(send=outbox, now=NOW)
        assert outbox.recipients == ["ann@acme.test"]


@pytest.mark.asyncio
async def test_a_refused_recipient_is_skipped_and_an_smtp_failure_backs_off_without_resending(monkeypatch):
    async with _database() as sessions:
        ids = await _users(
            sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True), ("cid@acme.test", "user", True)
        )
        await _pending(sessions, monkeypatch)
        ordered = sorted(ids, key=ids.get)  # recipients are served in user id order
        outbox = _Outbox(
            {
                ordered[0]: EmailDeliveryError("550 no such user", permanent=True),
                ordered[2]: EmailDeliveryError("421 try again later", permanent=False),
            }
        )

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        row = (await _rows(sessions))[-1]
        assert row.status == "pending" and row.failed_attempts == 1
        # The recipient the 421 hit was never taken: its reservation is released.
        assert await _outcomes(sessions, row.id) == {ids[ordered[0]]: "refused", ids[ordered[1]]: "delivered"}
        assert "421" in row.last_error
        retry_at = row.next_attempt_at
        assert retry_at > NOW

        # Not before the back-off, and then only to who is left.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(seconds=5)) == 0
        outbox.fail.pop(ordered[2])
        assert await mca.deliver_pending_announcements(send=outbox, now=retry_at) == 1
        assert outbox.recipients == [ordered[1], ordered[2]]
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts) == ("sent", 0)


@pytest.mark.asyncio
async def test_an_announcement_every_recipient_refused_fails(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        outbox = _Outbox({"ann@acme.test": EmailDeliveryError("550 no such user", permanent=True)})
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.last_error) == ("failed", "Every recipient refused the email")
        # Refused addresses, no email server error: the status reports the refusal alone.
        progress = await _progress(sessions)
        assert (progress.refused, progress.remaining, progress.failed_attempts, progress.last_error) == (1, 0, 0, None)


@pytest.mark.asyncio
async def test_failed_attempts_count_consecutive_failed_batches_then_give_up(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(mca, "MAX_ATTEMPTS", 3)
        first, second = sorted(ids, key=ids.get)
        outage = EmailDeliveryError("Connection refused", permanent=False)
        outbox = _Outbox({first: outage})

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts) == ("pending", 1)

        # A batch that got an email out before failing starts the count again.
        outbox.fail = {second: outage}
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 1
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts) == ("pending", 1)

        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts) == ("pending", 2)
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at) == ("failed", 3, None)
        assert outbox.recipients == [first]


def _refused_for_now(reply: str = "450 4.1.2 Domain not found") -> EmailDeliveryError:
    """What ``deliver_email`` raises when the server answers the address with a 4xx."""
    return EmailDeliveryError(
        reply,
        permanent=False,
        recipient_refused=True,
        address_specific=reply_names_the_address(reply),
    )


def _waits(row: ModelCatalogAnnouncement, start: datetime, delay: timedelta) -> bool:
    """``row`` runs next ``delay`` after a batch run at ``start`` (its clock moves with real time)."""
    return start + delay <= row.next_attempt_at < start + delay + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_an_address_refused_for_now_is_retried_once_after_everyone_else(monkeypatch):
    async with _database() as sessions:
        ids = await _users(
            sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True), ("cid@acme.test", "user", True)
        )
        await _pending(sessions, monkeypatch)
        ordered = sorted(ids, key=ids.get)
        outbox = _Outbox({ordered[0]: _refused_for_now(), ordered[1]: _refused_for_now("451 4.7.1 Greylisted")})

        # The batch goes on past them: the others are emailed.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        assert outbox.recipients == [ordered[2]]
        row = (await _rows(sessions))[-1]
        assert await _outcomes(sessions, row.id) == {
            ids[ordered[0]]: "deferred",
            ids[ordered[1]]: "deferred",
            ids[ordered[2]]: "delivered",
        }
        assert row.status == "pending" and row.next_attempt_at >= NOW + mca.DEFERRED_RETRY_DELAY
        # The wait for their retry is no failed batch and no email server error.
        assert row.failed_attempts == 0
        progress = await _progress(sessions)
        assert (progress.delivered, progress.refused, progress.remaining, progress.failed_attempts) == (1, 0, 2, 0)
        assert (progress.retry_at, progress.last_error, progress.held_until) == (None, None, None)
        assert (progress.deferred, progress.deferred_retry_at, progress.last_refusal) == (
            2,
            row.next_attempt_at,
            "451 4.7.1 Greylisted",
        )

        # Not before the delay; then each is tried once more and settled.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=1)) == 0
        outbox.fail.pop(ordered[1])
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 1
        assert outbox.recipients == [ordered[2], ordered[1]]
        row = (await _rows(sessions))[-1]
        # The retry's last refusal is not kept on the sent row.
        assert (row.status, row.last_error) == ("sent", None)
        assert await _outcomes(sessions, row.id) == {
            ids[ordered[0]]: "refused",
            ids[ordered[1]]: "delivered",
            ids[ordered[2]]: "delivered",
        }
        progress = await _progress(sessions)
        assert (progress.delivered, progress.refused, progress.remaining) == (2, 1, 0)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(hours=1)) == 0


@pytest.mark.asyncio
async def test_too_many_addresses_refused_for_now_in_a_row_stop_the_batch(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, *((f"user{i:02}@acme.test", "user", True) for i in range(12)))
        await _pending(sessions, monkeypatch)
        ordered = sorted(ids, key=ids.get)
        # The server refusing every address is the server refusing the platform.
        outbox = _Outbox({email: _refused_for_now("451 4.7.1 Sending rate exceeded") for email in ordered})

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        # Three set aside, the fourth released for the next batch, the rest never tried.
        assert await _outcomes(sessions, row.id) == {ids[email]: "deferred" for email in ordered[:3]}
        assert (row.status, row.failed_attempts) == ("pending", 1)
        assert NOW < row.next_attempt_at < NOW + mca.DEFERRED_RETRY_DELAY
        assert "Sending rate exceeded" in row.last_error

        # Batches that get nothing out back off as long as a failing server.
        for failures in (2, 3):
            at = row.next_attempt_at
            assert await mca.deliver_pending_announcements(send=outbox, now=at) == 0
            row = (await _rows(sessions))[-1]
            assert (row.status, row.failed_attempts) == ("pending", failures)
            assert _waits(row, at, mca._backoff(failures))
        assert mca._backoff(3) == timedelta(minutes=4)
        assert await _outcomes(sessions, row.id) == {ids[email]: "deferred" for email in ordered[:9]}

        outbox.fail = {}
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 3
        row = (await _rows(sessions))[-1]
        assert row.status == "pending" and row.next_attempt_at > NOW + mca.DEFERRED_RETRY_DELAY
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 9
        assert outbox.recipients == [*ordered[9:], *ordered[:9]]
        assert (await _rows(sessions))[-1].status == "sent"


@pytest.mark.asyncio
async def test_an_address_the_server_refuses_never_stops_a_batch_once_it_took_an_email(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, *((f"user{i:02}@acme.test", "user", True) for i in range(12)))
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 20)
        ordered = sorted(ids, key=ids.get)
        # A domain that does not resolve: the server names the address (4.1.2), at every attempt.
        dead = [*ordered[:4], *ordered[5:10]]
        outbox = _Outbox({email: _refused_for_now() for email in dead})

        # Before the server took a single email, a run of them may still be the
        # server refusing the platform (its DNS down): the batch stops and backs off.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert await _outcomes(sessions, row.id) == {ids[email]: "deferred" for email in ordered[:3]}
        assert (row.status, row.failed_attempts) == ("pending", 1)
        progress = await _progress(sessions)
        assert (progress.retry_at, progress.last_error, progress.last_refusal) == (
            row.next_attempt_at,
            "450 4.1.2 Domain not found",
            None,
        )

        # Once it took one, an address it refuses says nothing of it: the batch
        # goes on past every one of them.
        at = row.next_attempt_at
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 3
        row = (await _rows(sessions))[-1]
        assert await _outcomes(sessions, row.id) == {
            **{ids[email]: "deferred" for email in dead},
            **{ids[email]: "delivered" for email in (ordered[4], *ordered[10:])},
        }
        # Their retry is the only wait: no failed batch, no email server error.
        assert (row.status, row.failed_attempts) == ("pending", 0) and _waits(row, at, mca.DEFERRED_RETRY_DELAY)
        progress = await _progress(sessions)
        assert (progress.delivered, progress.remaining, progress.deferred) == (3, 9, 9)
        assert (progress.retry_at, progress.failed_attempts, progress.last_error, progress.held_until) == (
            None,
            0,
            None,
            None,
        )
        assert (progress.deferred_retry_at, progress.deferred_retrying, progress.last_refusal) == (
            row.next_attempt_at,
            False,
            "450 4.1.2 Domain not found",
        )

        # The one retry refuses all of them in one batch, and the announcement is
        # sent: the last refusal does not outlive the retry on the closed row.
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at, row.last_error) == ("sent", 0, None, None)
        assert await _outcomes(sessions, row.id) == {
            **{ids[email]: "refused" for email in dead},
            **{ids[email]: "delivered" for email in (ordered[4], *ordered[10:])},
        }
        progress = await _progress(sessions)
        assert (progress.delivered, progress.refused, progress.remaining, progress.deferred) == (3, 9, 0, 0)
        assert outbox.recipients == [ordered[4], *ordered[10:]]


@pytest.mark.asyncio
async def test_a_long_run_of_addresses_refused_since_the_last_email_backs_off_again(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, *((f"user{i:02}@acme.test", "user", True) for i in range(40)))
        await _pending(sessions, monkeypatch)
        # Smaller batches than the bound: the run is counted across them.
        monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 10)
        ordered = sorted(ids, key=ids.get)
        # The server takes the first email, then answers "domain not found" to
        # 30 addresses in a row (its DNS lost mid-run), then takes the rest.
        dead = ordered[1:31]
        outbox = _Outbox({email: _refused_for_now() for email in dead})
        assert mca._ADDRESS_REFUSALS_IN_A_ROW == 25

        # Below the bound, the batches go on past them with no failed batch.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=1)) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at) == ("pending", 0, None)

        # Past it, they count again: three more set aside, the fourth stops the batch.
        at = NOW + timedelta(minutes=2)
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 0
        row = (await _rows(sessions))[-1]
        assert await _outcomes(sessions, row.id) == {
            ids[ordered[0]]: "delivered",
            **{ids[email]: "deferred" for email in ordered[1:29]},
        }
        assert (row.status, row.failed_attempts) == ("pending", 1) and _waits(row, at, mca._backoff(1))
        progress = await _progress(sessions)
        assert (progress.retry_at, progress.last_error, progress.last_refusal) == (
            row.next_attempt_at,
            "450 4.1.2 Domain not found",
            None,
        )

        # The next batch still counts them, from the run the last ones left:
        # an email out ends it, and the rest goes out.
        at = row.next_attempt_at
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 8
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at) == ("pending", 0, None)
        at += timedelta(minutes=1)
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 1
        row = (await _rows(sessions))[-1]
        assert row.status == "pending" and _waits(row, at, mca.DEFERRED_RETRY_DELAY)
        assert await _outcomes(sessions, row.id) == {
            **{ids[email]: "deferred" for email in dead},
            **{ids[email]: "delivered" for email in (ordered[0], *ordered[31:])},
        }

        # The retries are not bounded: 30 in a row are refused batch after
        # batch, with no failed batch, and the status says they are under way.
        at = row.next_attempt_at
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at) == ("pending", 0, None)
        progress = await _progress(sessions)
        assert (progress.deferred, progress.deferred_retry_at, progress.deferred_retrying) == (20, None, True)
        assert (progress.retry_at, progress.last_error, progress.last_refusal) == (
            None,
            None,
            "450 4.1.2 Domain not found",
        )
        for minutes in (1, 2):
            assert await mca.deliver_pending_announcements(send=outbox, now=at + timedelta(minutes=minutes)) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts) == ("sent", 0)
        progress = await _progress(sessions)
        assert (progress.delivered, progress.refused, progress.remaining, progress.deferred) == (10, 30, 0, 0)
        assert progress.deferred_retrying is False


@pytest.mark.asyncio
async def test_a_batch_that_only_meets_addresses_refused_for_now_waits_and_the_retries_wait_once(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, *((f"user{i}@acme.test", "user", True) for i in range(5)))
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 2)
        ordered = sorted(ids, key=ids.get)
        outbox = _Outbox({email: _refused_for_now() for email in ordered[:3]})

        # Too small a batch for the run that stops one: it waits like a failed one.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts) == ("pending", 1) and _waits(row, NOW, mca._backoff(1))

        at = row.next_attempt_at
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 1
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at) == ("pending", 0, None)
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 1
        row = (await _rows(sessions))[-1]
        assert row.status == "pending" and _waits(row, at, mca.DEFERRED_RETRY_DELAY)

        # The wait comes once, before the first retries; the next batch goes on.
        outbox.fail = {}
        at = row.next_attempt_at
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 2
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.next_attempt_at) == ("pending", 0, None)
        progress = await _progress(sessions)
        assert (progress.retry_at, progress.held_until, progress.remaining) == (None, None, 1)
        # Everyone else was emailed: the retries are under way, not waiting for the others.
        assert (progress.deferred, progress.deferred_retry_at, progress.deferred_retrying) == (1, None, True)
        assert await mca.deliver_pending_announcements(send=outbox, now=at) == 1
        assert (await _rows(sessions))[-1].status == "sent"
        assert outbox.recipients == [*ordered[3:], *ordered[:3]]


@pytest.mark.asyncio
async def test_an_address_refused_for_now_never_holds_the_announcements_queued_behind_it(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        first, second = sorted(ids, key=ids.get)
        # The first address in id order, refused for now at every attempt.
        outbox = _Outbox({first: _refused_for_now()})
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        older = (await _rows(sessions))[-1]
        assert older.status == "pending"

        newer = await _boot(
            sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5", now=NOW + timedelta(seconds=10)
        )
        assert newer.id != older.id and newer.status == "pending"

        # The older one is retried once and closes; the newer one goes out after it.
        assert await mca.deliver_pending_announcements(send=outbox, now=older.next_attempt_at) == 0
        assert (await _rows(sessions))[-2].status == "sent"
        assert await _outcomes(sessions, older.id) == {ids[first]: "refused", ids[second]: "delivered"}
        assert await mca.deliver_pending_announcements(send=outbox, now=older.next_attempt_at) == 1
        assert await _outcomes(sessions, newer.id) == {ids[first]: "deferred", ids[second]: "delivered"}
        assert outbox.recipients == [second, second]


async def _progress(sessions) -> mca.AnnouncementProgress:
    async with sessions() as db:
        progress = await mca.latest_announcement(db)
    assert progress is not None
    return progress


@pytest.mark.asyncio
async def test_the_status_tells_how_far_an_announcement_got_and_why_it_waits(monkeypatch):
    async with _database() as sessions:
        ids = await _users(
            sessions,
            (ADMIN_EMAIL, "admin", True),
            ("ann@acme.test", "user", True),
            ("bob@acme.test", "user", True),
            ("cid@acme.test", "user", True),
            ("dan@acme.test", "user", False),
        )
        ordered = sorted((e for e in ids if e not in (ADMIN_EMAIL, "dan@acme.test")), key=ids.get)
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 1)

        progress = await _progress(sessions)
        assert (progress.row.status, progress.delivered, progress.remaining, progress.queued_behind) == (
            "pending",
            0,
            3,
            0,
        )
        assert (progress.retry_at, progress.failed_attempts, progress.last_error) == (None, 0, None)

        # The recipient being emailed is still to reach, never an unknown outcome.
        during: list[tuple[str, int, int, int]] = []

        async def _look(_to: str) -> None:
            seen = await _progress(sessions)
            during.append((seen.row.status, seen.delivered, seen.unknown, seen.remaining))

        outbox = _Outbox(on_send=_look)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        assert during == [("sending", 0, 0, 3)]
        progress = await _progress(sessions)
        assert (progress.row.status, progress.delivered, progress.remaining) == ("pending", 1, 2)

        # The server says "later": when the next batch runs, and why it waits.
        outbox.on_send = None
        busy = EmailDeliveryError("421 4.7.0 Too many messages,\n  try again later", permanent=False)
        outbox.fail = {ordered[1]: busy}
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=1)) == 0
        progress = await _progress(sessions)
        assert (progress.row.status, progress.delivered, progress.remaining) == ("pending", 1, 2)
        assert progress.retry_at == progress.row.next_attempt_at
        assert progress.retry_at > NOW + timedelta(minutes=1)
        assert progress.failed_attempts == 1
        assert progress.last_error == "421 4.7.0 Too many messages, try again later"

        # A batch that goes through clears it.
        outbox.fail = {}
        assert await mca.deliver_pending_announcements(send=outbox, now=progress.retry_at) == 1
        progress = await _progress(sessions)
        assert (progress.delivered, progress.remaining) == (2, 1)
        assert (progress.retry_at, progress.failed_attempts, progress.last_error) == (None, 0, None)

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(hours=1)) == 1
        progress = await _progress(sessions)
        assert (progress.row.status, progress.delivered, progress.remaining, progress.last_error) == (
            "sent",
            3,
            0,
            None,
        )
        assert outbox.recipients == ordered


@pytest.mark.asyncio
async def test_a_failed_announcement_reports_who_it_never_reached_and_the_last_error(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(mca, "MAX_ATTEMPTS", 1)
        first, _second = sorted(ids, key=ids.get)
        outbox = _Outbox({first: EmailDeliveryError("Connection refused " + "x" * 600, permanent=False)})

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        progress = await _progress(sessions)
        assert (progress.row.status, progress.delivered, progress.remaining, progress.failed_attempts) == (
            "failed",
            0,
            2,
            1,
        )
        assert progress.retry_at is None
        assert progress.last_error.startswith("Connection refused xxx") and progress.last_error.endswith("...")
        assert len(progress.last_error) == 300


@pytest.mark.asyncio
async def test_a_failed_announcement_counts_the_addresses_it_set_aside_as_refused(monkeypatch):
    async with _database() as sessions:
        ids = await _users(
            sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True), ("cid@acme.test", "user", True)
        )
        await _pending(sessions, monkeypatch)
        monkeypatch.setattr(mca, "MAX_ATTEMPTS", 1)
        first, second, _third = sorted(ids, key=ids.get)
        outbox = _Outbox({first: _refused_for_now(), second: EmailDeliveryError("Connection refused", permanent=False)})

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        progress = await _progress(sessions)
        # It will never be retried: refused, and not among the users it never reached.
        assert (progress.row.status, progress.refused, progress.remaining, progress.failed_attempts) == (
            "failed",
            1,
            2,
            1,
        )


@pytest.mark.asyncio
async def test_an_announcement_waiting_for_an_older_one_says_so(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True))
        await _pending(sessions, monkeypatch)
        older = (await _rows(sessions))[-1]
        async with sessions() as db:
            db.add(
                ModelCatalogAnnouncement(
                    source=mca.SOURCE_ADMIN,
                    created_at=older.created_at + timedelta(minutes=5),
                    models=older.models,
                    default_model=older.default_model,
                    default_provider=older.default_provider,
                    status="pending",
                )
            )
            await db.commit()

        progress = await _progress(sessions)
        assert (progress.row.source, progress.queued_behind, progress.remaining) == (mca.SOURCE_ADMIN, 1, 1)
        assert progress.blocked_until == older.next_attempt_at

        # The older one backs off: the newer one says when the queue moves.
        busy = EmailDeliveryError("421 4.7.0 Try again later", permanent=False)
        assert await mca.deliver_pending_announcements(send=_Outbox({"ann@acme.test": busy}), now=NOW) == 0
        older = (await _rows(sessions))[-2]
        assert older.next_attempt_at > NOW
        progress = await _progress(sessions)
        assert (progress.queued_behind, progress.blocked_until) == (1, older.next_attempt_at)
        # Its own state is untouched: no failed batch of its own, no hold.
        assert (progress.retry_at, progress.failed_attempts, progress.held_until) == (None, 0, None)

        # The older one goes first; then nothing is ahead of it.
        assert await mca.deliver_pending_announcements(send=_Outbox(), now=older.next_attempt_at) == 1
        progress = await _progress(sessions)
        assert (progress.queued_behind, progress.blocked_until) == (0, None)


@pytest.mark.asyncio
async def test_a_send_that_may_have_gone_out_is_never_retried(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        first, second = sorted(ids, key=ids.get)
        outbox = _Outbox({first: EmailDeliveryError("timed out", permanent=False, uncertain=True)})

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert row.status == "pending" and await _outcomes(sessions, row.id) == {ids[first]: "unknown"}

        outbox.fail = {}
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 1
        assert outbox.recipients == [second]
        row = (await _rows(sessions))[-1]
        assert row.status == "sent"
        assert await _outcomes(sessions, row.id) == {ids[first]: "unknown", ids[second]: "delivered"}


@pytest.mark.asyncio
async def test_an_unexpected_failure_backs_off_and_never_resends(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        first, second = sorted(ids, key=ids.get)
        outbox = _Outbox({first: RuntimeError("boom")})

        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.failed_attempts, row.last_error) == ("pending", 1, "boom")

        outbox.fail = {}
        assert await mca.deliver_pending_announcements(send=outbox, now=row.next_attempt_at) == 1
        assert outbox.recipients == [second]
        assert await _outcomes(sessions, row.id) == {ids[first]: "unknown", ids[second]: "delivered"}


@pytest.mark.asyncio
async def test_a_send_a_dead_worker_reserved_is_never_sent_again(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        first, second = sorted(ids, key=ids.get)

        # A worker claims the row, reserves its first recipient, and dies
        # before it can say whether the email went out.
        row_id, source, token, _, batch = await mca._claim(NOW)
        assert [r.email for r in batch] == [first, second]
        async with sessions() as db:
            assert await mca._reserve(db, row_id, source, token, batch[0], NOW) is True

        outbox = _Outbox()
        # Its lease still holds: nobody else sends.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=1)) == 0
        # Then it runs out, and the next worker skips the send it never settled.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + mca.CLAIM_LEASE) == 1
        assert outbox.recipients == [second]
        row = (await _rows(sessions))[-1]
        assert row.status == "sent"
        assert await _outcomes(sessions, row_id) == {ids[first]: "unknown", ids[second]: "delivered"}


@pytest.mark.asyncio
async def test_a_worker_whose_lease_ran_out_stops_and_leaves_the_row_to_the_next(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        row_id, source, late_token, _, batch = await mca._claim(NOW)
        _, _, token, _, _ = await mca._claim(NOW + mca.CLAIM_LEASE)
        assert token != late_token

        async with sessions() as db:
            assert await mca._reserve(db, row_id, source, late_token, batch[0], NOW + mca.CLAIM_LEASE) is None
        await mca._finish(row_id, late_token, sent=0, error="421 try again later", at=NOW + mca.CLAIM_LEASE)

        row = (await _rows(sessions))[-1]
        assert (row.status, row.claim_token, row.failed_attempts, row.last_error) == ("sending", token, 0, None)
        assert await _outcomes(sessions, row_id) == {}


@pytest.mark.asyncio
async def test_expiry_closes_a_row_a_dead_worker_left_sending(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        row_id, source, token, _, batch = await mca._claim(NOW)
        async with sessions() as db:
            assert await mca._reserve(db, row_id, source, token, batch[0], NOW) is True

        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + mca.MAX_AGE + timedelta(hours=1)) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.claim_token, row.next_attempt_at) == ("expired", None, None)
        assert await _outcomes(sessions, row_id) == {ids["ann@acme.test"]: "unknown"}
        assert outbox.sent == []


@pytest.mark.asyncio
async def test_announcements_go_out_in_order_and_one_already_sent_to_someone_is_never_folded(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        first, second = sorted(ids, key=ids.get)
        outbox = _Outbox({second: EmailDeliveryError("421 try again later", permanent=False)})
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        older = (await _rows(sessions))[-1]
        assert older.status == "pending" and older.next_attempt_at > NOW

        # Opus 5.5 lands while the Sonnet 5.5 email is half out: a row of its own.
        newer = await _boot(
            sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5", now=NOW + timedelta(seconds=10)
        )
        assert newer.id != older.id and newer.status == "pending"

        # The newer one waits for the older one, even while it backs off.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(seconds=20)) == 0
        assert await _outcomes(sessions, newer.id) == {}

        outbox.fail = {}
        assert await mca.deliver_pending_announcements(send=outbox, now=older.next_attempt_at) == 1
        assert await mca.deliver_pending_announcements(send=outbox, now=older.next_attempt_at) == 2
        assert outbox.recipients == [first, second, first, second]
        assert [m["subject"].split(" is now")[0] for m in outbox.sent] == [
            "Claude Sonnet 5.5",
            "Claude Sonnet 5.5",
            "Claude Opus 5.5",
            "Claude Opus 5.5",
        ]
        assert [r.status for r in await _rows(sessions)] == ["baseline", "sent", "sent"]


@pytest.mark.asyncio
async def test_a_user_deactivated_during_the_batch_is_not_emailed(monkeypatch):
    async with _database() as sessions:
        ids = await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        first, second = sorted(ids, key=ids.get)

        async def deactivate_the_other(_to: str) -> None:
            async with sessions() as db:
                user = await db.get(User, uuid.UUID(ids[second]))
                user.is_active = False
                await db.commit()

        outbox = _Outbox(on_send=deactivate_the_other)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        assert outbox.recipients == [first]
        assert (await _rows(sessions))[-1].status == "sent"


@pytest.mark.asyncio
async def test_a_claimed_row_is_left_to_its_claimant_until_the_lease_expires(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        async with sessions() as db:
            row = (
                await db.execute(select(ModelCatalogAnnouncement).where(ModelCatalogAnnouncement.status == "pending"))
            ).scalar_one()
            row.status, row.next_attempt_at = "sending", NOW + mca.CLAIM_LEASE
            await db.commit()
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + mca.CLAIM_LEASE) == 1


@pytest.mark.asyncio
async def test_an_announcement_held_too_long_expires_and_one_nobody_can_receive_is_skipped(monkeypatch):
    async with _database() as sessions:
        await _pending(sessions, monkeypatch)
        outbox = _Outbox()
        # No recipient at all.
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        row = (await _rows(sessions))[-1]
        assert (row.status, row.skip_reason) == ("skipped", "no_recipients")

    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + mca.MAX_AGE + timedelta(hours=1)) == 0
        assert (await _rows(sessions))[-1].status == "expired"
        assert outbox.sent == []


async def _queue_admin_default(sessions, model: str) -> None:
    async with sessions() as db:
        await mca.record_default_model_change(
            db,
            previous_default="claude-sonnet-5-5",
            new_default=model,
            new_provider="anthropic",
            announce=True,
            now=NOW + timedelta(minutes=1),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_a_process_that_cannot_send_leaves_the_queued_announcement_to_one_that_can(monkeypatch, caplog):
    """The rolling deploy: a pod of the previous release runs the scheduler tick first."""
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        outbox = _Outbox()

        monkeypatch.setattr(settings, "model_catalog_emails_enabled", False)
        monkeypatch.setattr(settings, "smtp_hostname", "")
        monkeypatch.setattr(mca, "_smtp_missing_reported_at", None)
        with caplog.at_level(logging.WARNING, logger=mca.logger.name):
            assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        assert "1 queued announcement(s) left to another process" in caplog.text
        assert (await _rows(sessions))[-1].status == "pending"

        # The switch is the boot's to read: a process without it still delivers
        # what the boot queued.
        monkeypatch.setattr(settings, "smtp_hostname", "smtp.acme.test")
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 1
        assert outbox.recipients == ["ann@acme.test"]


@pytest.mark.asyncio
async def test_a_boot_with_the_emails_off_closes_the_env_announcements_only(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        await _queue_admin_default(sessions, "claude-haiku-4-5")

        monkeypatch.setattr(settings, "model_catalog_emails_enabled", False)
        await _apply_boot(sessions, now=NOW + timedelta(minutes=2))
        assert [(r.source, r.status, r.skip_reason) for r in await _rows(sessions)] == [
            ("env", "baseline", None),
            ("env", "skipped", "disabled"),
            ("admin", "pending", None),
        ]


@pytest.mark.asyncio
async def test_turning_the_emails_off_closes_every_queued_announcement_and_sends_nothing(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        await _queue_admin_default(sessions, "claude-haiku-4-5")
        outbox = _Outbox()

        # A process started with the switch delivers nothing, whatever is queued.
        monkeypatch.setattr(settings, "model_announcement_emails_disabled", True)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=2)) == 0
        assert [r.status for r in await _rows(sessions)] == ["baseline", "pending", "pending"]

        # Its boot closes the env AND the admin rows.
        await _apply_boot(sessions, now=NOW + timedelta(minutes=2))
        rows = await _rows(sessions)
        assert [(r.source, r.status, r.skip_reason) for r in rows] == [
            ("env", "baseline", None),
            ("env", "skipped", "turned_off"),
            ("admin", "skipped", "turned_off"),
        ]
        assert all(r.claim_token is None for r in rows)

        # Turned back on: the env change skipped for the switch alone goes out.
        monkeypatch.setattr(settings, "model_announcement_emails_disabled", False)
        await _apply_boot(sessions, now=NOW + timedelta(minutes=3))
        env_row = (await _rows(sessions))[1]
        assert (env_row.status, env_row.skip_reason) == ("pending", None)
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=3)) == 1
        assert outbox.recipients == ["ann@acme.test"]


@pytest.mark.asyncio
async def test_a_boot_without_smtp_closes_every_queued_announcement(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        await _queue_admin_default(sessions, "claude-haiku-4-5")
        # A worker still holding the env row stops at its next recipient.
        async with sessions() as db:
            row = await db.scalar(
                select(ModelCatalogAnnouncement).where(
                    ModelCatalogAnnouncement.source == "env", ModelCatalogAnnouncement.status == "pending"
                )
            )
            row.status, row.claim_token = "sending", uuid.uuid4()
            await db.commit()

        monkeypatch.setattr(settings, "smtp_hostname", "")
        await _apply_boot(sessions, now=NOW + timedelta(minutes=2))
        rows = await _rows(sessions)
        assert [(r.source, r.status, r.skip_reason) for r in rows] == [
            ("env", "baseline", None),
            ("env", "skipped", "smtp_not_configured"),
            ("admin", "skipped", "smtp_not_configured"),
        ]
        assert all(r.claim_token is None for r in rows)


@pytest.mark.asyncio
async def test_every_outcome_is_logged_at_warning(monkeypatch, caplog):
    """Production runs LOG_LEVEL=warning: each once-per-change outcome must show."""
    caplog.set_level(logging.WARNING, logger=mca.logger.name)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        async with sessions() as db:
            await mca._log_recorded(db, (await _rows(sessions))[-1])
        assert "emailing 1 user(s) from the next scheduler tick" in caplog.text
        assert await mca.deliver_pending_announcements(send=_Outbox(), now=NOW) == 1
        assert "sent to 1 user(s) (0 refused, 0 unknown)" in caplog.text

    caplog.clear()
    async with _database() as sessions:
        await _pending(sessions, monkeypatch)
        assert await mca.deliver_pending_announcements(send=_Outbox(), now=NOW) == 0
        assert "not sent: no user to email" in caplog.text

    caplog.clear()
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pending(sessions, monkeypatch)
        assert await mca.deliver_pending_announcements(send=_Outbox(), now=NOW + mca.MAX_AGE + timedelta(hours=1)) == 0
        assert "expired unsent" in caplog.text

    caplog.clear()
    async with _database() as sessions:
        skipped = await _skipped_rollout(sessions, monkeypatch)
        async with sessions() as db:
            await mca._log_recorded(db, skipped)
        assert "the users are not emailed, MODEL_CATALOG_EMAILS_ENABLED is off" in caplog.text
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        await _apply_boot(sessions, now=NOW + timedelta(hours=1))
        assert "the change skipped at an earlier boot" in caplog.text


# --- Admin announcements (Settings > AI and models > Models) --------------------------------------


async def _admin_saves_default(
    sessions, previous: str, new: str, *, announce: bool, now: datetime = NOW
) -> ModelCatalogAnnouncement | None:
    """What ``PUT /settings/general-assistant`` does: the setting and its announcement, one commit."""
    async with sessions() as db:
        row = await mca.record_default_model_change(
            db, previous_default=previous, new_default=new, new_provider="anthropic", announce=announce, now=now
        )
        ps = await get_platform_settings(db)
        ps.agent_default_model, ps.agent_default_provider = new, "anthropic"
        await db.commit()
        return row


@pytest.mark.asyncio
async def test_an_admin_announcement_goes_to_every_user_whatever_the_env_settings(monkeypatch):
    async with _database() as sessions:
        await _users(
            sessions,
            (ADMIN_EMAIL, "admin", True),
            ("ann@acme.test", "admin", True),
            ("bob@acme.test", "user", True),
        )
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        # The env audience and switch govern the env seed's announcements only.
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", False)
        monkeypatch.setattr(settings, "model_catalog_emails_recipients", "admins")
        row = await _admin_saves_default(sessions, "claude-sonnet-5-5", "claude-haiku-4-5", announce=True)
        assert (row.source, row.status, row.previous_default_model) == ("admin", "pending", "claude-sonnet-5-5")
        async with sessions() as db:
            assert await mca.announcement_recipient_count(db) == 2

        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 2
        assert sorted(outbox.recipients) == ["ann@acme.test", "bob@acme.test"]
        assert "Claude Haiku 4.5 is now your default model" in outbox.sent[0]["subject"]
        assert "Replaces Claude Sonnet 5.5" in outbox.sent[0]["text"]
        assert (await _rows(sessions))[-1].status == "sent"


@pytest.mark.asyncio
async def test_saving_the_default_again_before_the_email_went_out_replaces_it(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5")
        first = await _admin_saves_default(sessions, "claude-sonnet-5-5", "claude-haiku-4-5", announce=True)
        second = await _admin_saves_default(
            sessions, "claude-haiku-4-5", "claude-opus-5-5", announce=True, now=NOW + timedelta(minutes=1)
        )
        # Nobody heard of Haiku: the news is Sonnet 5.5 -> Opus 5.5.
        assert second.previous_default_model == "claude-sonnet-5-5"
        assert [(r.id, r.status) for r in await _rows(sessions)] == [(first.id, "superseded"), (second.id, "pending")]

        # Back to where the users last heard: nothing to tell.
        assert (
            await _admin_saves_default(
                sessions, "claude-opus-5-5", "claude-sonnet-5-5", announce=True, now=NOW + timedelta(minutes=2)
            )
            is None
        )
        assert [r.status for r in await _rows(sessions)] == ["superseded", "superseded"]


@pytest.mark.asyncio
async def test_an_admin_announcement_of_a_default_changed_since_is_dropped_unsent(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        await _admin_saves_default(sessions, "claude-sonnet-5-5", "claude-haiku-4-5", announce=True)
        # The env seed of the next rollout puts its own default back before the tick.
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")

        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        assert outbox.sent == []
        assert (await _rows(sessions))[-1].status == "superseded"


@pytest.mark.asyncio
async def test_email_users_announces_an_env_managed_default_once(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _skipped_rollout(sessions, monkeypatch)

        async with sessions() as db:
            row = await mca.announce_current_default_model(db, now=NOW + timedelta(minutes=5))
            await db.commit()
        # The replaced default comes from what the env seed recorded.
        assert (row.source, row.default_model, row.previous_default_model) == (
            "admin",
            "claude-sonnet-5-5",
            "claude-sonnet-5",
        )
        async with sessions() as db:
            with pytest.raises(mca.AnnouncementRefusedError, match="already being announced"):
                await mca.announce_current_default_model(db, now=NOW + timedelta(minutes=6))

        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW + timedelta(minutes=10)) == 1
        assert "Claude Sonnet 5.5 is now your default model" in outbox.sent[0]["subject"]

        monkeypatch.setattr(settings, "smtp_hostname", "")
        async with sessions() as db:
            with pytest.raises(mca.AnnouncementRefusedError, match="SMTP"):
                await mca.announce_current_default_model(db, now=NOW + timedelta(minutes=20))


@pytest.mark.asyncio
async def test_the_env_seed_never_folds_into_or_measures_against_an_admin_announcement(monkeypatch):
    async with _database() as sessions:
        await _pending(sessions, monkeypatch)
        await _queue_admin_default(sessions, "claude-haiku-4-5")

        row = await _boot(
            sessions, [*SONNET_55, "claude-opus-5-5"], "claude-sonnet-5-5", now=NOW + timedelta(minutes=2)
        )
        # Folded into the env rollout nobody was told about yet, not into the admin row.
        change = mca.announcement_change(row)
        assert [m.id for m in change.added] == ["claude-opus-5-5"]
        assert change.new_default == mca.CatalogModel("claude-sonnet-5-5", "anthropic")
        assert [(r.source, r.status) for r in await _rows(sessions)] == [
            ("env", "baseline"),
            ("env", "pending"),
            ("admin", "pending"),
        ]


# --- Admin catalog changes (a provider saved in Settings) -------------------------------


async def _admin_edits(
    sessions, models: list[str], *, provider: str = "openai", now: datetime = NOW
) -> mca.CatalogDraft | None:
    """What a provider save in Settings does: the edit and the change it records, one commit."""
    async with sessions() as db:
        before = await mca.snapshot_catalogs(db)
        row = (
            await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.provider == provider))
        ).scalar_one_or_none()
        if row is None:
            row = LLMProviderConfig(
                provider=provider, api_key_enc=encrypt_value("sk-test"), is_from_env=False, user_id=None
            )
            db.add(row)
        row.available_models = list(models)
        await db.flush()
        change = await mca.record_provider_edit(db, before, now=now)
        await db.commit()
        return change


async def _offer_behind_the_admins_back(sessions, provider: str, models: list[str]) -> None:
    """The models change without a provider save (the env seed, another replica)."""
    async with sessions() as db:
        await db.execute(
            update(LLMProviderConfig).where(LLMProviderConfig.provider == provider).values(available_models=models)
        )
        await db.commit()


def _news(row: ModelCatalogAnnouncement | mca.CatalogDraft | None) -> tuple[list[str], list[str]]:
    if row is None:
        return [], []
    change = row.change if isinstance(row, mca.CatalogDraft) else mca.announcement_change(row)
    return [m.id for m in change.added], [m.id for m in change.removed]


async def _draft(sessions, *, now: datetime = NOW) -> mca.CatalogDraft | None:
    async with sessions() as db:
        return await mca.catalog_draft(db, now=now)


async def _announce(
    sessions, added: list[str], removed: list[str], *, draft: mca.CatalogDraft | None, now: datetime
) -> ModelCatalogAnnouncement:
    """The admin emails the users the models they chose, from the draft they reviewed."""
    async with sessions() as db:
        row = await mca.announce_model_changes(
            db, added=added, removed=removed, draft_id=draft.version.id if draft else None, now=now
        )
        await db.commit()
        return row


def _minutes(n: int) -> datetime:
    return NOW + timedelta(minutes=n)


@pytest.mark.asyncio
async def test_every_provider_save_is_kept_and_the_admin_is_offered_their_net(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        draft = await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini", "text-embedding-3-small"])
        # What the users pick from: the chat models only.
        assert _news(draft) == (["gpt-5.5", "gpt-5.5-mini"], [])
        assert (draft.saves, draft.first_saved_at, draft.version.created_at) == (1, NOW, NOW)
        assert (draft.version.source, draft.version.status, draft.version.default_model) == ("admin", "recorded", None)
        assert [m.provider for m in draft.change.added] == ["openai", "openai"]

        # Recorded is not queued: nothing goes out until the admin says so.
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=NOW) == 0
        assert outbox.sent == []

        # A save that changes nothing the users pick from records nothing.
        assert await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini"], now=_minutes(1)) is None
        assert len(await _rows(sessions)) == 1

        # Back and forth: a model added then retired is no news, offered again it is.
        assert _news(await _admin_edits(sessions, ["gpt-5.5"], now=_minutes(2))) == (["gpt-5.5"], [])
        assert _news(await _admin_edits(sessions, [], now=_minutes(3))) == ([], [])
        assert await _draft(sessions, now=_minutes(3)) is None
        again = await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini"], now=_minutes(4))
        assert _news(again) == (["gpt-5.5", "gpt-5.5-mini"], [])

        # A model offered before the saves, retired then offered again, is no news;
        # retired, it is - whichever provider offers it, beside the other saves.
        await _admin_edits(sessions, ["claude-sonnet-5-5"], provider="anthropic", now=_minutes(5))
        restored = await _admin_edits(sessions, SONNET_55, provider="anthropic", now=_minutes(6))
        assert _news(restored) == (["gpt-5.5", "gpt-5.5-mini"], [])
        retired = await _admin_edits(sessions, ["claude-sonnet-5-5"], provider="anthropic", now=_minutes(7))
        assert _news(retired) == (["gpt-5.5", "gpt-5.5-mini"], ["claude-haiku-4-5"])
        assert retired.change.removed == (mca.CatalogModel("claude-haiku-4-5", "anthropic"),)
        assert (retired.saves, retired.first_saved_at) == (7, NOW)
        assert [r.status for r in await _rows(sessions) if r.source == "admin"] == ["recorded"] * 7


@pytest.mark.asyncio
async def test_the_draft_survives_edits_the_platform_never_saw(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini"])
        # Retired without a provider save (another replica, the env seed): the draft
        # stops saying it is new, and the next save keeps the rest.
        await _offer_behind_the_admins_back(sessions, "openai", ["gpt-5.5"])
        assert _news(await _draft(sessions, now=_minutes(1))) == (["gpt-5.5"], [])
        draft = await _admin_edits(sessions, ["gpt-5.4", "gpt-5.5"], now=_minutes(2))
        assert _news(draft) == (["gpt-5.4", "gpt-5.5"], [])
        # Offered again the same way: the save that added it still says so.
        await _offer_behind_the_admins_back(sessions, "openai", ["gpt-5.4", "gpt-5.5", "gpt-5.5-mini"])
        assert _news(await _draft(sessions, now=_minutes(3))) == (["gpt-5.4", "gpt-5.5", "gpt-5.5-mini"], [])


@pytest.mark.asyncio
async def test_the_draft_covers_the_saves_of_the_configured_window(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        await _admin_edits(sessions, ["gpt-5.4"], now=NOW - timedelta(hours=23))
        draft = await _admin_edits(sessions, ["gpt-5.4", "gpt-5.5"])
        assert _news(draft) == (["gpt-5.4", "gpt-5.5"], [])
        assert (draft.saves, draft.first_saved_at) == (2, NOW - timedelta(hours=23))

        # Past the window a save is no longer offered, whether it was emailed or not.
        assert _news(await _draft(sessions, now=NOW + timedelta(hours=2))) == (["gpt-5.5"], [])
        monkeypatch.setattr(settings, "model_catalog_changes_window_hours", 1)
        shorter = await _draft(sessions, now=_minutes(30))
        assert (_news(shorter), shorter.saves, shorter.first_saved_at) == ((["gpt-5.5"], []), 1, NOW)
        assert await _draft(sessions, now=NOW + timedelta(hours=1)) is None

        # A save stamped before the latest one (a pod's clock behind) still comes after it.
        late = await _admin_edits(sessions, ["gpt-5.5"], now=NOW - timedelta(minutes=5))
        assert late.version.created_at > draft.version.created_at
        assert _news(late) == (["gpt-5.5"], ["gpt-5.4"])


@pytest.mark.asyncio
async def test_the_admin_chooses_the_models_the_email_announces(monkeypatch):
    async with _database() as sessions:
        await _users(
            sessions,
            (ADMIN_EMAIL, "admin", True),
            ("ann@acme.test", "admin", True),
            ("bob@acme.test", "user", True),
        )
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        # gpt-5.3 was retired days ago, never announced and out of the window.
        await _admin_edits(sessions, ["gpt-5.3"], now=NOW - timedelta(days=3))
        await _admin_edits(sessions, ["gpt-5.4"], now=NOW - timedelta(days=2))
        draft = await _admin_edits(sessions, ["gpt-5.4", "gpt-5.5", "gpt-5.5-mini"])
        assert _news(draft) == (["gpt-5.5", "gpt-5.5-mini"], [])
        async with sessions() as db:
            offered = await mca.read_offered_models(db)
            assert await mca.formerly_offered_models(db, offered) == (mca.CatalogModel("gpt-5.3", "openai"),)

        # New: any model the users pick, in the saves or not; retired: any other.
        monkeypatch.setattr(settings, "model_catalog_emails_enabled", False)
        monkeypatch.setattr(settings, "model_catalog_emails_recipients", "admins")
        row = await _announce(
            sessions,
            [" gpt-5.5 ", "gpt-5.4", "claude-haiku-4-5", "gpt-5.5", ""],
            ["gpt-5.3", "legacy-model"],
            draft=draft,
            now=_minutes(1),
        )
        change = mca.announcement_change(row)
        assert (row.source, row.status) == ("admin", "pending")
        assert change.added == (
            mca.CatalogModel("claude-haiku-4-5", "anthropic"),
            mca.CatalogModel("gpt-5.4", "openai"),
            mca.CatalogModel("gpt-5.5", "openai"),
        )
        assert change.removed == (mca.CatalogModel("gpt-5.3", "openai"), mca.CatalogModel("legacy-model", ""))
        # The saves reviewed are consumed, the model left out included.
        assert await _draft(sessions, now=_minutes(1)) is None
        with pytest.raises(mca.AnnouncementRefusedError, match="already emailed or queued"):
            await _announce(sessions, ["gpt-5.5"], [], draft=draft, now=_minutes(2))

        # The env audience and switch govern the env seed's announcements only.
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(3)) == 2
        assert sorted(outbox.recipients) == ["ann@acme.test", "bob@acme.test"]
        message = outbox.sent[0]
        assert message["subject"] == f"New models available in your {mca_platform_name()} platform"
        assert "Claude Haiku 4.5" in message["text"]
        assert "GPT-5.3" in message["text"]
        assert "legacy-model" in message["text"]
        assert "GPT-5.5 mini" not in message["text"]

        # An announcement of the admin's own, with no saves behind it.
        own = await _announce(sessions, ["gpt-5.5-mini"], [], draft=None, now=_minutes(4))
        assert _news(own) == (["gpt-5.5-mini"], [])


@pytest.mark.asyncio
async def test_a_selection_that_cannot_be_announced_is_refused(monkeypatch):
    async with _database() as sessions:
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        draft = await _admin_edits(sessions, ["gpt-5.5"])
        refusals = [
            (["gpt-9"], [], "gpt-9 is not offered"),
            (["gpt-9", "gpt-10"], [], "gpt-9, gpt-10 are not offered"),
            (["gpt-5.5"], [" gpt-5.5"], "both as new and as retired: gpt-5.5"),
            ([], [mca.CatalogModel("old-model", "acme")], "Unknown provider for old-model: acme"),
            ([" ", ""], [], "at least one"),
            (["gpt-5.5\n"], ["bad\x00model"], "must be a model id"),
            (["x" * (mca.MODEL_ID_MAX_CHARS + 1)], [], "printable"),
            ([], [f"old-{i}" for i in range(mca.MAX_ANNOUNCED_MODELS + 1)], "At most"),
        ]
        for added, removed, reason in refusals:
            with pytest.raises(mca.AnnouncementSelectionError, match=reason):
                await _announce(sessions, added, removed, draft=draft, now=_minutes(1))
        # Nothing consumed, nothing queued.
        assert _news(await _draft(sessions, now=_minutes(1))) == (["gpt-5.5"], [])
        assert [r.status for r in await _rows(sessions)] == ["recorded"]

        async with sessions() as db:
            with pytest.raises(LookupError):
                await mca.announce_model_changes(db, added=["gpt-5.5"], removed=[], draft_id=uuid.uuid4())
        monkeypatch.setattr(settings, "smtp_hostname", "")
        with pytest.raises(mca.AnnouncementRefusedError, match="SMTP"):
            await _announce(sessions, ["gpt-5.5"], [], draft=draft, now=_minutes(1))


@pytest.mark.asyncio
async def test_any_model_can_be_announced_as_retired_the_ones_still_offered_included(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        await _admin_edits(sessions, ["gpt-5.3"], now=NOW - timedelta(days=3))
        await _admin_edits(sessions, ["gpt-5.5"], now=NOW - timedelta(days=2))
        # Retired ahead of time (still offered), under a provider named by hand,
        # last recorded by the platform, or never seen by it at all.
        row = await _announce(
            sessions,
            [],
            [
                "claude-haiku-4-5",
                mca.CatalogModel("gpt-4o", " Azure_OpenAI "),
                "gpt-5.3",
                mca.CatalogModel("mystery-model", ""),
            ],
            draft=None,
            now=NOW,
        )
        assert mca.announcement_change(row).removed == (
            mca.CatalogModel("claude-haiku-4-5", "anthropic"),
            mca.CatalogModel("gpt-4o", "azure_openai"),
            mca.CatalogModel("gpt-5.3", "openai"),
            mca.CatalogModel("mystery-model", ""),
        )
        assert [entry["id"] for entry in row.previous_models if entry.get("offered") is True] == ["claude-haiku-4-5"]

        # Nobody told yet: a save sends it back to the draft, which keeps the choice.
        folded = await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini"], now=_minutes(1))
        assert _news(folded) == (["gpt-5.5-mini"], ["claude-haiku-4-5", "gpt-4o", "gpt-5.3", "mystery-model"])
        queued = await _announce(
            sessions, ["gpt-5.5-mini"], ["claude-haiku-4-5", "gpt-5.3"], draft=folded, now=_minutes(2)
        )

        # Still offered is what the admin chose: it goes out.
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(3)) == 1
        assert {r.id: r.status for r in await _rows(sessions)}[queued.id] == "sent"
        assert "Claude Haiku 4.5" in outbox.sent[0]["text"]
        assert "GPT-5.3" in outbox.sent[0]["text"]

        # A retired model that was not offered and comes back is news gone stale.
        stale = await _announce(sessions, [], ["gpt-5.3"], draft=None, now=_minutes(4))
        await _offer_behind_the_admins_back(sessions, "openai", ["gpt-5.3", "gpt-5.5", "gpt-5.5-mini"])
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(5)) == 0
        assert {r.id: r.status for r in await _rows(sessions)}[stale.id] == "superseded"


@pytest.mark.asyncio
async def test_a_save_made_after_the_review_stays_for_the_next_email(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        reviewed = await _admin_edits(sessions, ["gpt-5.5"])
        later = await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini"], now=_minutes(1))
        await _announce(sessions, ["gpt-5.5"], [], draft=reviewed, now=_minutes(2))
        remaining = await _draft(sessions, now=_minutes(2))
        assert (_news(remaining), remaining.saves, remaining.version.id) == (
            (["gpt-5.5-mini"], []),
            1,
            later.version.id,
        )


@pytest.mark.asyncio
async def test_a_queued_email_sent_to_nobody_goes_back_to_the_draft(monkeypatch):
    monkeypatch.setattr(settings, "model_catalog_emails_batch_size", 1)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True), ("bob@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        draft = await _admin_edits(sessions, ["gpt-5.5", "gpt-5.5-mini"])
        queued = await _announce(sessions, ["gpt-5.5"], [], draft=draft, now=_minutes(1))

        # Nobody told yet: the admin reviews the queued email with the new save.
        folded = await _admin_edits(sessions, ["gpt-5.4", "gpt-5.5", "gpt-5.5-mini"], now=_minutes(2))
        assert (_news(folded), folded.saves) == ((["gpt-5.4", "gpt-5.5"], []), 2)
        statuses = {r.id: r.status for r in await _rows(sessions)}
        assert statuses[queued.id] == "recorded"
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(3)) == 0

        # Reaching the users already: it goes on, and a new save is a draft of its own.
        sending = await _announce(sessions, ["gpt-5.4", "gpt-5.5"], [], draft=folded, now=_minutes(4))
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(5)) == 1
        own = await _admin_edits(sessions, ["gpt-5.4", "gpt-5.5", "gpt-5.5-mini", "gpt-5.5-nano"], now=_minutes(6))
        assert _news(own) == (["gpt-5.5-nano"], [])
        assert {r.id: r.status for r in await _rows(sessions)}[sending.id] in mca._ACTIVE
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(20)) == 1
        assert sorted(outbox.recipients) == ["ann@acme.test", "bob@acme.test"]


@pytest.mark.asyncio
async def test_a_queued_change_that_no_longer_holds_is_never_emailed(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        draft = await _admin_edits(sessions, ["gpt-5.4", "gpt-5.5", "gpt-5.5-mini"])
        queued = await _announce(sessions, ["gpt-5.5-mini"], [], draft=draft, now=_minutes(1))
        # Retired behind the admin's back before the tick: dropped unsent.
        await _offer_behind_the_admins_back(sessions, "openai", ["gpt-5.4", "gpt-5.5"])
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=_minutes(2)) == 0
        assert outbox.sent == []
        assert {r.id: r.status for r in await _rows(sessions)}[queued.id] == "superseded"


@pytest.mark.asyncio
async def test_an_admin_edit_of_the_env_catalog_is_not_announced_again_at_the_next_boot(monkeypatch):
    async with _database() as sessions:
        await _boot(sessions, SONNET_55, "claude-sonnet-5-5")
        # LLM_PROVIDER_MODELS_EDITABLE: the admin retires an env-seeded model.
        row = await _admin_edits(sessions, ["claude-sonnet-5-5"], provider="anthropic", now=NOW + timedelta(minutes=1))
        assert _news(row) == ([], ["claude-haiku-4-5"])
        env = [r for r in await _rows(sessions) if r.source == "env"]
        assert env[-1].status == "recorded"
        ledger = mca.announcement_change(env[-1])
        assert ([m.id for m in ledger.removed], ledger.new_default) == (["claude-haiku-4-5"], None)

        # The next boot keeps the admin's edit: nothing new for the env to announce.
        async with sessions() as db:
            catalog = await mca.read_model_catalog(db)
            assert await mca.record_catalog_change(db, catalog, catalog, now=NOW + timedelta(hours=1)) is None
        assert [r for r in await _rows(sessions) if r.status == "pending"] == []


# --- Routes -----------------------------------------------------------------------------


@asynccontextmanager
async def _admin_client(sessions, monkeypatch, email: str = "ann@acme.test"):
    """The settings routes as the app mounts them, called by an admin, agentic AI on."""
    import httpx
    from fastapi import FastAPI

    from app.api import deps
    from app.api.v1 import model_announcements as announcements_api
    from app.api.v1 import settings as settings_api

    async def _ai_on(*_args, **_kwargs) -> None:
        return None

    async def _no_rate_limit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(settings_api, "require_agentic_ai", _ai_on)
    monkeypatch.setattr(announcements_api, "enforce_rate_limit", _no_rate_limit)
    async with sessions() as db:
        admin = await db.scalar(select(User).where(User.email == email))
    app = FastAPI()
    app.include_router(announcements_api.router, prefix="/api/v1/settings/model-announcements")
    app.include_router(settings_api.router, prefix="/api/v1/settings")
    app.dependency_overrides[deps.require_admin] = lambda: admin
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_saving_a_new_default_with_the_email_queues_it_with_the_setting(monkeypatch):
    # An instance whose default is the admin's to choose (no DEFAULT_MODEL pin).
    monkeypatch.setattr(settings, "default_model", "")
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True), ("bob@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        async with _admin_client(sessions, monkeypatch) as client:
            unchanged = await client.put(
                "/api/v1/settings/general-assistant",
                json={"agent_default_model": "claude-sonnet-5-5", "announce_default_model": True},
            )
            assert unchanged.status_code == 422, unchanged.text

            response = await client.put(
                "/api/v1/settings/general-assistant",
                json={
                    "agent_default_model": "claude-haiku-4-5",
                    "agent_default_provider": "anthropic",
                    "announce_default_model": True,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["default_model_announced"] is True

            status_response = await client.get("/api/v1/settings/model-announcements")
            assert status_response.status_code == 200, status_response.text
            body = status_response.json()
            assert (body["email_configured"], body["recipient_count"], body["default_model"]) == (
                True,
                2,
                "claude-haiku-4-5",
            )
            assert body["previous_default_model"] == "claude-sonnet-5-5"
            assert (body["latest"]["source"], body["latest"]["status"]) == ("admin", "pending")
            assert body["emails_per_minute"] == 20
            latest = body["latest"]
            assert (latest["delivered"], latest["remaining"], latest["queued_behind"]) == (0, 2, 0)
            assert (latest["retry_at"], latest["failed_attempts"], latest["last_error"]) == (None, 0, None)

            # Saved without the email: the queued one is stale and dropped.
            response = await client.put(
                "/api/v1/settings/general-assistant",
                json={"agent_default_model": "claude-sonnet-5-5", "agent_default_provider": "anthropic"},
            )
            assert response.status_code == 200, response.text
            assert response.json().get("default_model_announced") is None
        assert [r.status for r in await _rows(sessions)] == ["superseded"]


@pytest.mark.asyncio
async def test_the_test_email_goes_to_the_calling_admin_only(monkeypatch):
    from app.services import email as email_service

    sent: list[dict] = []

    async def _deliver(**kwargs) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(email_service, "deliver_email", _deliver)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True), ("bob@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        async with _admin_client(sessions, monkeypatch) as client:
            # The default about to be saved, replacing the current one.
            response = await client.post(
                "/api/v1/settings/model-announcements/test",
                json={"model": "claude-haiku-4-5", "provider": "anthropic"},
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"sent_to": "ann@acme.test"}
            # The current default, as Email users would announce it.
            assert (await client.post("/api/v1/settings/model-announcements/test", json={})).status_code == 200
            refused = await client.post("/api/v1/settings/model-announcements/test", json={"model": "not-a-model"})
            assert refused.status_code == 422, refused.text

            monkeypatch.setattr(settings, "smtp_hostname", "")
            assert (await client.post("/api/v1/settings/model-announcements/test", json={})).status_code == 409

        assert [message["to"] for message in sent] == ["ann@acme.test", "ann@acme.test"]
        assert sent[0]["subject"].startswith(mca.TEST_SUBJECT_PREFIX)
        assert "Claude Haiku 4.5 is now your default model" in sent[0]["subject"]
        assert "Replaces Claude Sonnet 5.5" in sent[0]["text"]
        assert "Claude Sonnet 5.5 is now your default model" in sent[1]["subject"]
        assert await _rows(sessions) == []


@pytest.mark.asyncio
async def test_email_users_queues_the_current_default_and_refuses_a_second_one(monkeypatch):
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True), ("bob@acme.test", "user", True))
        await _skipped_rollout(sessions, monkeypatch)
        async with _admin_client(sessions, monkeypatch) as client:
            status_body = (await client.get("/api/v1/settings/model-announcements")).json()
            latest = status_body["latest"]
            assert (latest["source"], latest["status"], latest["skip_reason"]) == ("env", "skipped", "disabled")
            assert latest["removed_models"] == ["claude-sonnet-5"]

            response = await client.post("/api/v1/settings/model-announcements")
            assert response.status_code == 202, response.text
            body = response.json()
            assert (body["source"], body["status"], body["default_model"], body["previous_default_model"]) == (
                "admin",
                "pending",
                "claude-sonnet-5-5",
                "claude-sonnet-5",
            )
            again = await client.post("/api/v1/settings/model-announcements")
            assert again.status_code == 409, again.text


@pytest.mark.asyncio
async def test_every_admin_email_is_refused_while_the_emails_are_turned_off(monkeypatch):
    from app.services import email as email_service

    sent: list[dict] = []

    async def _deliver(**kwargs) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(email_service, "deliver_email", _deliver)
    monkeypatch.setattr(settings, "default_model", "")
    monkeypatch.setattr(settings, "model_announcement_emails_disabled", True)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True), ("bob@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        async with _admin_client(sessions, monkeypatch) as client:
            body = (await client.get("/api/v1/settings/model-announcements")).json()
            assert (body["email_configured"], body["emails_disabled"]) == (True, True)

            refusals = [
                await client.post("/api/v1/settings/model-announcements/test", json={}),
                await client.post("/api/v1/settings/model-announcements"),
                await client.post(
                    "/api/v1/settings/model-announcements/model-changes",
                    json={"added_models": ["claude-haiku-4-5"], "removed_models": []},
                ),
                await client.put(
                    "/api/v1/settings/general-assistant",
                    json={
                        "agent_default_model": "claude-haiku-4-5",
                        "agent_default_provider": "anthropic",
                        "announce_default_model": True,
                    },
                ),
            ]
            assert [(r.status_code, r.json()["detail"]) for r in refusals] == [(409, mca.TURNED_OFF_DETAIL)] * 4

            # Saving the default without the email still works, recorded only.
            saved = await client.put(
                "/api/v1/settings/general-assistant",
                json={"agent_default_model": "claude-haiku-4-5", "agent_default_provider": "anthropic"},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json().get("default_model_announced") is None

        assert sent == []
        assert [r.status for r in await _rows(sessions)] == ["recorded"]


async def _openai_config(sessions, models: list[str]) -> str:
    async with sessions() as db:
        row = LLMProviderConfig(
            provider="openai",
            api_key_enc=encrypt_value("sk-test"),
            is_from_env=False,
            user_id=None,
            available_models=list(models),
        )
        db.add(row)
        await db.commit()
        return str(row.id)


@pytest.mark.asyncio
async def test_the_saved_model_changes_are_edited_before_they_are_emailed(monkeypatch):
    from app.services import email as email_service

    sent: list[dict] = []

    async def _deliver(**kwargs) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(email_service, "deliver_email", _deliver)
    monkeypatch.setattr(settings, "models_env_providing_only", False)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True), ("bob@acme.test", "user", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        config_id = await _openai_config(sessions, ["gpt-5.4"])
        url = f"/api/v1/settings/llm-providers/{config_id}"
        async with _admin_client(sessions, monkeypatch) as client:
            response = await client.put(url, json={"available_models": ["gpt-5.5"]})
            assert response.status_code == 200, response.text
            first = response.json()["model_catalog_change"]
            assert (first["added_models"], first["removed_models"], first["saves"]) == (["gpt-5.5"], ["gpt-5.4"], 1)
            assert first["added"] == [{"id": "gpt-5.5", "provider": "openai"}]

            # Every save of the window is kept: the draft is their net.
            response = await client.put(url, json={"available_models": ["gpt-5.5", "gpt-5.5-mini"]})
            change = response.json()["model_catalog_change"]
            assert (change["added_models"], change["removed_models"]) == (["gpt-5.5", "gpt-5.5-mini"], ["gpt-5.4"])
            assert (change["saves"], change["first_saved_at"]) == (2, first["created_at"])
            assert change["id"] != first["id"]

            # A rename changes nothing the users pick from.
            renamed = await client.put(url, json={"display_name": "OpenAI (prod)"})
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["model_catalog_change"] is None

            status_body = (await client.get("/api/v1/settings/model-announcements")).json()
            assert status_body["unannounced_change"] == change
            assert (status_body["latest"], status_body["changes_window_hours"]) == (None, 24)
            assert status_body["offered_models"] == [
                {"id": "claude-haiku-4-5", "provider": "anthropic"},
                {"id": "claude-sonnet-5-5", "provider": "anthropic"},
                {"id": "gpt-5.5", "provider": "openai"},
                {"id": "gpt-5.5-mini", "provider": "openai"},
            ]
            assert status_body["formerly_offered_models"] == [{"id": "gpt-5.4", "provider": "openai"}]

            # The test email shows the selection the admin is about to send.
            selection = {"added_models": ["gpt-5.5", "claude-haiku-4-5"], "removed_models": ["gpt-5.4"]}
            test = await client.post("/api/v1/settings/model-announcements/test", json=selection)
            assert test.status_code == 200, test.text
            for refused in (
                {**selection, "model": "claude-haiku-4-5"},
                {"added_models": ["gpt-9"]},
                {"added_models": [], "removed_models": []},
                # A retired model is free text the email names as is: a model id, never a sentence (#5045).
                {"removed_models": ["Your account is locked, sign in at evil.example"]},
            ):
                response = await client.post("/api/v1/settings/model-announcements/test", json=refused)
                assert response.status_code == 422, response.text

            # The preview is the users' email, rendered and never sent.
            preview = await client.post("/api/v1/settings/model-announcements/preview", json=selection)
            assert preview.status_code == 200, preview.text
            email = preview.json()
            assert email["subject"] == f"New models available in your {mca_platform_name()} platform"
            assert "Claude Haiku 4.5" in email["text"] and "GPT-5.4" in email["text"]
            assert "Hello Josh," in email["text"] and "Claude Haiku 4.5" in email["html"]
            refused = await client.post(
                "/api/v1/settings/model-announcements/preview", json={"added_models": ["gpt-9"]}
            )
            assert refused.status_code == 422, refused.text

            send = "/api/v1/settings/model-announcements/model-changes"
            chosen = {
                "draft_id": change["id"],
                "added_models": ["gpt-5.5", "claude-haiku-4-5"],
                # Any model as retired: offered still, named with its provider, or unknown.
                "removed_models": ["gpt-5.4", {"id": "gpt-4o", "provider": "azure_openai"}, "gpt-5.5-mini"],
            }
            for refused in (
                {**chosen, "added_models": ["gpt-9"]},
                {**chosen, "removed_models": ["gpt-5.5"]},
                {**chosen, "removed_models": [{"id": "gpt-4o", "provider": "acme"}]},
                {**chosen, "removed_models": [{"id": "x" * 256}]},
                {**chosen, "removed_models": [{"id": "Reset your password at evil.example", "provider": "openai"}]},
            ):
                response = await client.post(send, json=refused)
                assert response.status_code == 422, response.text
            assert (await client.post(send, json={**chosen, "draft_id": str(uuid.uuid4())})).status_code == 404
            assert (await client.post(send, json={**chosen, "draft_id": "not-an-id"})).status_code == 404

            queued = await client.post(send, json=chosen)
            assert queued.status_code == 202, queued.text
            body = queued.json()
            assert (body["source"], body["status"], body["added_models"], body["removed_models"]) == (
                "admin",
                "pending",
                ["claude-haiku-4-5", "gpt-5.5"],
                ["gpt-4o", "gpt-5.4", "gpt-5.5-mini"],
            )
            assert body["remaining"] == 2
            again = await client.post(send, json=chosen)
            assert again.status_code == 409, again.text

            status_body = (await client.get("/api/v1/settings/model-announcements")).json()
            assert status_body["unannounced_change"] is None
            assert (status_body["latest"]["id"], status_body["latest"]["status"]) == (body["id"], "pending")

        assert [message["to"] for message in sent] == ["ann@acme.test"]
        assert sent[0]["subject"].startswith(mca.TEST_SUBJECT_PREFIX)
        assert "Claude Haiku 4.5" in sent[0]["text"]
        assert "GPT-5.4" in sent[0]["text"]
        assert "GPT-5.5 mini" not in sent[0]["text"]
        queued_row = next(r for r in await _rows(sessions) if str(r.id) == body["id"])
        assert mca.announcement_change(queued_row).removed == (
            mca.CatalogModel("gpt-4o", "azure_openai"),
            mca.CatalogModel("gpt-5.4", "openai"),
            mca.CatalogModel("gpt-5.5-mini", "openai"),
        )


@pytest.mark.asyncio
async def test_a_deleted_provider_leaves_its_retired_models_to_email(monkeypatch):
    monkeypatch.setattr(settings, "models_env_providing_only", False)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        config_id = await _openai_config(sessions, ["gpt-5.5", "gpt-5.5-mini"])
        async with _admin_client(sessions, monkeypatch) as client:
            response = await client.delete(f"/api/v1/settings/llm-providers/{config_id}")
            assert response.status_code == 204, response.text
            change = (await client.get("/api/v1/settings/model-announcements")).json()["unannounced_change"]
            assert (change["added_models"], change["removed_models"]) == ([], ["gpt-5.5", "gpt-5.5-mini"])
            assert change["removed"] == [
                {"id": "gpt-5.5", "provider": "openai"},
                {"id": "gpt-5.5-mini", "provider": "openai"},
            ]


@pytest.mark.asyncio
async def test_every_provider_is_asked_which_models_it_serves_and_one_failing_costs_its_own_list(monkeypatch):
    import asyncio

    from app.llm import router as llm_router

    class _Serves:
        def __init__(self, api_key: str) -> None:
            assert api_key == "sk-test"

        async def list_models(self) -> list[str]:
            return ["gpt-5.5", "gpt-4o", "gpt-4.1", "text-embedding-3-small", "", "bad\x00id", "gpt-4o"]

    class _Refuses:
        def __init__(self, api_key: str) -> None:
            pass

        async def list_models(self) -> list[str]:
            raise RuntimeError("401 invalid key")

    class _Hangs(_Refuses):
        async def list_models(self) -> list[str]:
            await asyncio.sleep(30)
            return ["gemini-3-pro"]

    monkeypatch.setitem(llm_router.PROVIDER_MAP, "openai", _Serves)
    monkeypatch.setitem(llm_router.PROVIDER_MAP, "anthropic", _Refuses)
    monkeypatch.setitem(llm_router.PROVIDER_MAP, "gemini", _Hangs)
    monkeypatch.setitem(llm_router.PROVIDER_MAP, "tei", _Refuses)
    monkeypatch.setattr(mca, "PROVIDER_CATALOG_TIMEOUT_SECONDS", 0.2)
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "admin", True))
        await _seed(sessions, SONNET_55, "claude-sonnet-5-5")
        openai_id = await _openai_config(sessions, ["gpt-5.5"])
        async with sessions() as db:
            gemini = LLMProviderConfig(
                provider="gemini",
                display_name=" Gemini (EU) ",
                api_key_enc=encrypt_value("sk-test"),
                is_from_env=False,
                user_id=None,
                available_models=[],
            )
            # An embedding server serves no model a user picks: never asked.
            embeddings = LLMProviderConfig(
                provider="tei", api_key_enc=encrypt_value("sk-test"), is_from_env=False, user_id=None
            )
            db.add_all([gemini, embeddings])
            await db.commit()
            gemini_id = str(gemini.id)
        async with _admin_client(sessions, monkeypatch) as client:
            response = await client.get("/api/v1/settings/model-announcements/catalogs")
            assert response.status_code == 200, response.text
            body = response.json()
        # The chat models the users cannot pick; offered ones and embeddings are not listed.
        assert body["models"] == [
            {"id": "gpt-4.1", "provider": "openai"},
            {"id": "gpt-4o", "provider": "openai"},
        ]
        providers = {entry["provider"]: entry for entry in body["providers"]}
        assert [entry["provider"] for entry in body["providers"]] == ["anthropic", "openai", "gemini"]
        assert providers["openai"] == {
            "provider_id": openai_id,
            "provider": "openai",
            "label": None,
            "model_count": 3,
            "error": None,
            "truncated": False,
        }
        assert (providers["anthropic"]["model_count"], providers["anthropic"]["error"]) == (0, "401 invalid key")
        assert providers["gemini"] == {
            "provider_id": gemini_id,
            "provider": "gemini",
            "label": "Gemini (EU)",
            "model_count": 0,
            "error": "no answer within 0.2 seconds",
            "truncated": False,
        }


# --- Capabilities ----------------------------------------------------------


def _capability_env(monkeypatch, **values: str) -> None:
    """The capability defaults of the pod's environment, every other one empty."""
    from app.llm.env_capability_defaults import ROLES

    names = ["default_image_provider", "default_image_model", "default_image_provider_base_url"]
    for role in ROLES:
        names += [role.provider_env.lower(), role.model_env.lower()] + ([role.voice_env.lower()] if role.voice_env else [])
    for name in names:
        monkeypatch.setattr(settings, name, values.get(name, ""))
    monkeypatch.setattr(settings, "byollm_activated", False)


SAAS_IMAGE = {"default_image_provider": "openai", "default_image_model": "gpt-image-2"}
SAAS_MEDIA = {
    **SAAS_IMAGE,
    "default_video_provider": "gemini",
    "default_speech_provider": "openai",
    "default_speech_model": "tts-1-hd",
    "default_music_provider": "gemini",
    "default_music_model": "lyria-3.5",
}


@pytest.mark.asyncio
async def test_the_saas_upgrade_announces_the_capabilities_it_brings_and_not_the_image_model_it_had(
    monkeypatch, video_studio_enabled
):
    async with _database() as sessions:
        # An install an older release recorded: its rows hold no capabilities.
        _prod_env(monkeypatch, "claude-sonnet-5,claude-haiku-4-5")
        _capability_env(monkeypatch, **SAAS_IMAGE)
        await _env_boot()
        async with sessions() as db:
            await db.execute(update(ModelCatalogAnnouncement).values(capabilities=None, previous_capabilities=None))
            await db.commit()

        # The release and the platform rollout: Gemini, and the media models pinned.
        monkeypatch.setenv("LLM_PROVIDER_GEMINI_API_KEY", "AIza-test")
        _capability_env(monkeypatch, **SAAS_MEDIA)
        await _env_boot()

        row = (await _rows(sessions))[-1]
        assert row.status == "pending", (row.status, row.skip_reason)
        change = mca.announcement_change(row)
        assert [(c.capability, c.model, c.previous_model) for c in change.capabilities] == [
            ("music", "lyria-3.5", None),
            ("speech", "tts-1-hd", None),
            ("video", VIDEO_DEFAULTS["gemini"], None),
        ]
        assert change.added == () and change.new_default is None

        await _users(sessions, ("ann@acme.test", "user", True))
        outbox = _Outbox()
        assert await mca.deliver_pending_announcements(send=outbox, now=datetime.now(UTC) + timedelta(minutes=1)) == 1
        assert outbox.sent[0]["subject"].startswith("New AI capabilities in your ")

        # The next replica boots on the same environment: nothing new to say.
        await _env_boot()
        assert (await _rows(sessions))[-1].id == row.id


@pytest.mark.asyncio
async def test_a_capability_moving_to_a_newer_model_is_announced(monkeypatch, video_studio_enabled):
    async with _database() as sessions:
        _prod_env(monkeypatch, "claude-sonnet-5,claude-haiku-4-5")
        monkeypatch.setenv("LLM_PROVIDER_GEMINI_API_KEY", "AIza-test")
        _capability_env(monkeypatch, default_video_provider="gemini", default_video_model="veo-3.1-generate-preview")
        await _env_boot()

        _capability_env(monkeypatch, default_video_provider="gemini", default_video_model="veo-4.0-generate-preview")
        await _env_boot()

        row = (await _rows(sessions))[-1]
        assert row.status == "pending"
        (news,) = mca.announcement_change(row).capabilities
        assert (news.capability, news.model, news.provider, news.previous_model) == (
            "video",
            "veo-4.0-generate-preview",
            "gemini",
            "veo-3.1-generate-preview",
        )


@pytest.mark.asyncio
async def test_a_fresh_install_with_capabilities_announces_nothing(monkeypatch, video_studio_enabled):
    async with _database() as sessions:
        _prod_env(monkeypatch, "claude-sonnet-5,claude-haiku-4-5")
        monkeypatch.setenv("LLM_PROVIDER_GEMINI_API_KEY", "AIza-test")
        _capability_env(monkeypatch, **SAAS_MEDIA)
        await _env_boot()

        assert [r.status for r in await _rows(sessions)] == ["baseline", "recorded"]
        assert {c["capability"] for c in (await _rows(sessions))[-1].capabilities} == {"image", "video", "speech", "music"}


@pytest.mark.asyncio
async def test_the_video_and_music_models_are_announced_only_once_the_video_studio_is_on(monkeypatch):
    """They serve the Video Studio alone: while it is off the users are
    not told about models they cannot use, and turning it on tells them."""
    monkeypatch.setattr(settings, "enabled_features", "")
    async with _database() as sessions:
        _prod_env(monkeypatch, "claude-sonnet-5,claude-haiku-4-5")
        _capability_env(monkeypatch, **SAAS_IMAGE)
        await _env_boot()

        monkeypatch.setenv("LLM_PROVIDER_GEMINI_API_KEY", "AIza-test")
        _capability_env(monkeypatch, **SAAS_MEDIA)
        await _env_boot()

        row = (await _rows(sessions))[-1]
        assert row.status == "pending", (row.status, row.skip_reason)
        assert [(c.capability, c.model) for c in mca.announcement_change(row).capabilities] == [("speech", "tts-1-hd")]
        assert {c["capability"] for c in row.capabilities} == {"image", "speech"}

        monkeypatch.setattr(settings, "enabled_features", "VIDEO_STUDIO")
        await _env_boot()

        latest = (await _rows(sessions))[-1]
        assert {c.capability for c in mca.announcement_change(latest).capabilities} >= {"music", "video"}


async def _pin_image(sessions, monkeypatch) -> None:
    """``DEFAULT_IMAGE_MODEL`` and the image model the seed stored for it."""
    _capability_env(monkeypatch, **SAAS_IMAGE)
    async with sessions() as db:
        (await get_platform_settings(db)).image_model = SAAS_IMAGE["default_image_model"]
        await db.commit()


async def _forget_capabilities(sessions) -> None:
    """The rows as a release that did not track capabilities recorded them."""
    async with sessions() as db:
        await db.execute(update(ModelCatalogAnnouncement).values(capabilities=None, previous_capabilities=None))
        await db.commit()


@pytest.mark.asyncio
async def test_a_change_pending_since_an_older_release_takes_the_capabilities_the_next_deploy_brings(
    monkeypatch, video_studio_enabled
):
    """The Sonnet 5.5 rollout, recorded by a release that did not track
    capabilities, has reached nobody when the next deploy pins the video model:
    one email says both, and the image model the platform already had is not
    news."""
    async with _database() as sessions:
        await _pin_image(sessions, monkeypatch)
        await _seed(sessions, SONNET_5, "claude-sonnet-5")
        monkeypatch.setattr(settings, "default_model", "claude-sonnet-5-5")
        first = await _boot(sessions, SONNET_55, "claude-sonnet-5-5")
        assert first.status == "pending"
        await _forget_capabilities(sessions)

        _capability_env(monkeypatch, **SAAS_IMAGE, default_video_provider="gemini")
        before = await _catalog(sessions)
        async with sessions() as db:
            gemini = LLMProviderConfig(provider="gemini", api_key_enc=encrypt_value("AIza-test"), is_from_env=True)
            db.add(gemini)
            await db.flush()
            ps = await get_platform_settings(db)
            ps.video_provider, ps.video_provider_id, ps.video_model = "gemini", gemini.id, VIDEO_DEFAULTS["gemini"]
            await db.commit()
        after = await _catalog(sessions)
        async with sessions() as db:
            folded = await mca.record_catalog_change(db, before, after, now=NOW + timedelta(minutes=10))

        assert (folded.id, folded.status) == (first.id, "pending")
        assert [entry["capability"] for entry in folded.previous_capabilities] == ["image"]
        change = mca.announcement_change(folded)
        assert change.new_default == mca.CatalogModel("claude-sonnet-5-5", "anthropic")
        assert change.capabilities == (mca.CapabilityNews("video", VIDEO_DEFAULTS["gemini"], "gemini"),)


@pytest.mark.asyncio
async def test_a_change_skipped_by_an_older_release_is_sent_once_a_boot_can_send_it(monkeypatch):
    """A skipped rollout an older release recorded holds no capabilities: the
    image model the platform has now does not make it stale, and it says nothing
    of capabilities."""
    async with _database() as sessions:
        await _users(sessions, ("ann@acme.test", "user", True))
        await _pin_image(sessions, monkeypatch)
        skipped = await _skipped_rollout(sessions, monkeypatch)
        await _forget_capabilities(sessions)

        monkeypatch.setattr(settings, "model_catalog_emails_enabled", True)
        await _apply_boot(sessions, now=NOW + timedelta(hours=1))

        row = (await _rows(sessions))[-1]
        assert (row.id, row.status, row.skip_reason) == (skipped.id, "pending", None)
        change = mca.announcement_change(row)
        assert change.new_default == mca.CatalogModel("claude-sonnet-5-5", "anthropic")
        assert change.capabilities == ()


@pytest.mark.asyncio
async def test_a_capability_the_environment_stops_pinning_is_not_announced(monkeypatch):
    async with _database() as sessions:
        _prod_env(monkeypatch, "claude-sonnet-5,claude-haiku-4-5")
        monkeypatch.setenv("LLM_PROVIDER_GEMINI_API_KEY", "AIza-test")
        _capability_env(monkeypatch, **SAAS_MEDIA)
        await _env_boot()

        _capability_env(monkeypatch, **SAAS_IMAGE)
        await _env_boot()

        row = (await _rows(sessions))[-1]
        assert row.status == "recorded"
        assert not mca.announcement_change(row).announces
