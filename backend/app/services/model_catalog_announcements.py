# SPDX-License-Identifier: Apache-2.0

"""Tell a platform's users when its env-managed model catalog changes.

On SaaS deployments the model catalog is pinned by env vars
(``LLM_PROVIDER_<P>_MODELS`` and ``DEFAULT_MODEL``) and rolled out fleet-wide.
When the catalog a boot seeds differs from the one the platform last knew, this
module records the change, and the scheduler emails the platform's users what
it brings: the new default model, the models now available, the ones retired.

Two halves around two tables (``model_catalog_announcements`` and, per user
emailed, ``model_catalog_announcement_recipients``):

* **Record, at the env seed.** :func:`model_catalog_watch` wraps the seed in
  ``provision_env_providers`` - run at boot and when AI is switched on at
  runtime, both under ``bootstrap_lock()``, which serializes it across workers
  and replicas. It reads the catalog before and after the seed,
  and :func:`record_catalog_change` compares the result with the LATEST
  recorded row (the live pre-seed catalog only when no row exists yet), so a
  crash between the seed and the record is caught at the next boot. A fresh
  install (an empty catalog before) announces nothing. A change recorded while
  the previous one is still unsent to anyone is folded into it: one email per
  net change, none for a change reverted before it went out. A queued env
  change is held until the env catalog stayed unchanged for
  ``MODEL_CATALOG_EMAILS_SETTLE_MINUTES`` (``next_attempt_at``), each fold
  restarting the wait, so the changes of successive deploys share one email.
* **Decide, at the boot too** (:func:`apply_boot_configuration`). Whether a
  change may go out is decided by the configuration of the process that
  booted, never by the one delivering: during a rolling deploy the scheduler
  tick may still run in a pod started with the previous environment (without
  the switch or SMTP the rollout adds), and it must not close the row the new
  boot queued. A boot with the emails off closes the open env rows; a boot
  without SMTP closes every open row; a boot with both revives its latest env
  change when it was skipped for the configuration alone, is younger than
  ``MAX_AGE``, was emailed to nobody and describes the live catalog - the
  switch turned on after the rollout sends the news it was about.
* **Deliver, from the scheduler tick** (:func:`deliver_pending_announcements`).
  Rows go out in the order they were recorded: only the oldest open row is
  claimed (``FOR UPDATE SKIP LOCKED``, a lease in ``next_attempt_at`` and a
  ``claim_token``), a newer one waits behind it. Each recipient of the bounded
  batch is RESERVED (a recipient row, ``sending``) in a committed transaction
  before its email is sent, and settled after it (``delivered``, ``refused``,
  ``deferred``, or released when the server took nothing), so a user is emailed
  at most once whatever crashes: a reservation a dead worker left is settled
  ``unknown``, never sent again, and a worker whose lease ran out finds its
  claim token gone at the next reservation and stops. No transaction is open
  while SMTP runs. A refused recipient is skipped; one the server refuses for
  now is skipped and retried once ``DEFERRED_RETRY_DELAY`` after everyone else
  was emailed, so one address never holds the announcement (nor the ones queued
  behind it), and that wait is no failed batch: it is reported as the retry of
  those addresses, never as an email server error. Any other failure,
  SMTP or not, stops the batch and backs off,
  longer for each batch in a row that got no email out. A row gives up after
  ``MAX_ATTEMPTS`` consecutive failed batches (an email out starts the count
  again), and expires
  ``MAX_AGE`` after the change. A process without SMTP sends nothing and
  leaves the rows to one that has it.

What became of a change (queued, skipped and why, sent to how many, dropped) is
logged at WARNING: production runs ``LOG_LEVEL=warning``, and an operator who
rolled out a catalog must be able to see it in the logs.

The catalog is what users pick from: the chat-capable models an env-seeded
provider's env list names, and the platform default when ``DEFAULT_MODEL`` pins
it. It also holds what the users' agents create with: the image,
video, speech, music and decision models, each while the environment pins it
(``env_capability_defaults.pinned_choices``) and the users can use it (the video
and music models only while the Video Studio is on, ``capability_offered``).
A capability that arrives, or whose
model changes, is announced; one the environment stops pinning is not, since
the administrator's or the automatic choice takes over. A row recorded before
the capabilities were tracked holds none (NULL): the boot then counts as known
the ones its seed left as they were stored, so what a platform already had is
never announced, and announces the others as newly available
(``_known_catalog``). Routing, fallback and embedding models are never
announced.

**Admin announcements.** An admin who changes the default model in
Settings chooses whether to email the users about it, and may announce the
current default on purpose (the way to send a change recorded while the emails
were off). A provider edit that adds or retires a model the users pick from (a
model list saved, a provider added or deleted) is recorded too, one row per save,
and the admin is offered the net of the saves of
``MODEL_CATALOG_CHANGES_WINDOW_HOURS`` (:func:`catalog_draft`), which they edit
before sending: any model the users can pick as new, any model at all as retired
(:func:`announce_model_changes`). Those rows carry ``source = 'admin'``: a
default row the default before and after, a catalog row no default and only the
models added and retired. The env record compares only with ``env`` rows, so they
never make a boot announce a change (and an admin edit of an env-seeded catalog
is recorded as the env baseline, so the next boot does not announce it either).
They go out through the same delivery, to every active user (the admin's choice
is the opt-in: ``MODEL_CATALOG_EMAILS_ENABLED`` and
``MODEL_CATALOG_EMAILS_RECIPIENTS`` govern the env rows only), and never go out
stale: a save made while one is queued and emailed to nobody puts it back in the
draft, and the claim drops one whose news no longer holds.

**Turned off.** ``MODEL_ANNOUNCEMENT_EMAILS_DISABLED`` turns every model
announcement email off, the env ones and the admin ones alike: an env change is
recorded ``skipped`` (``turned_off``), a boot with it on closes every open row
unsent, a process with it on delivers nothing, and an admin announcement or
test email is refused (:func:`refuse_unless_emails_allowed`). The changes are
still recorded, so a boot after it is turned back off revives the latest env
change like any other configuration skip.

Statuses: ``baseline`` (the first state recorded, never sent), ``recorded`` (a
change with nothing to announce, or a fresh install's first catalog), ``pending`` / ``sending`` / ``sent``,
``skipped`` (see ``skip_reason``), ``superseded`` (folded away), ``expired``,
``failed``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.model_catalog_announcement import ModelCatalogAnnouncement, ModelCatalogAnnouncementRecipient

logger = logging.getLogger(__name__)

SOURCE_ENV = "env"
SOURCE_ADMIN = "admin"
TEST_SUBJECT_PREFIX = "[Test] "

MAX_ATTEMPTS = 50
MAX_AGE = timedelta(days=7)
CLAIM_LEASE = timedelta(minutes=10)
_BACKOFF_BASE = timedelta(minutes=1)
_BACKOFF_MAX = timedelta(hours=1)
# An address the server refuses for now (a 4xx to the address: greylisting, an
# unknown domain, a full mailbox) is retried once, this long after everyone
# else was emailed, then counted refused.
DEFERRED_RETRY_DELAY = timedelta(minutes=10)
# More addresses than this refused for now in a row, with no email out between
# them, is the server refusing the platform (a sending limit), not those
# addresses: the batch stops and backs off.
#
# A refusal whose code names the address itself (``EmailDeliveryError.
# address_specific``: no such domain, no such mailbox, a full one) is not
# counted once the server took an email of the announcement: it refused that
# address, not the platform, and a run of test or dead addresses - which the
# retry pass is made of - would otherwise stop every batch and back off.
# Before the server took one it is counted like any other refusal, so a relay
# answering "domain not found" to every address (its DNS down) backs off
# rather than setting every user aside.
#
# A sending limit still in force when the retries run refuses them as a dead
# domain would, and up to this many a batch are then counted refused. Accepted:
# the retries are the addresses that answered a 4xx, so a run of dead ones is
# likely there, and keeping a retry until an email goes out would stop on that
# run at every batch, holding every newer announcement until MAX_ATTEMPTS.
_DEFERRALS_IN_A_ROW = 3
# Past this many addresses refused for now in a row since the server last took
# an email of the announcement, across batches, an address's own refusal counts
# again: dead addresses sit between the users the server takes, while a relay
# that loses its DNS mid-run answers "domain not found" to every one of them,
# and must back off rather than set every user left aside. The retries are not
# bounded: they are the addresses refused for now, back to back by
# construction, so a run of them says nothing of the server (see above).
_ADDRESS_REFUSALS_IN_A_ROW = 25

_PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
    "mistral": "Mistral AI",
    "xai": "xAI",
    "azure_openai": "Azure OpenAI",
    "bedrock": "AWS Bedrock",
    "litellm": "LiteLLM",
    "custom_openai": "Custom OpenAI-compatible",
    "filigran_gateway": "Filigran AI Gateway",
    "lmstudio": "LM Studio",
}


# --- The catalog -----------------------------------------------------------------


@dataclass(frozen=True, order=True)
class CatalogModel:
    id: str
    provider: str


@dataclass(frozen=True, order=True)
class CapabilityModel:
    """The model one capability runs on: ``image``, ``video``, ``speech``, ``music``, ``decision``."""

    capability: str
    model: str
    provider: str = ""


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[CatalogModel, ...] = ()
    default_model: str | None = None
    default_provider: str | None = None
    # One per capability the environment pins.
    capabilities: tuple[CapabilityModel, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.models and not self.default_model and not self.capabilities

    def ids(self) -> set[str]:
        return {model.id for model in self.models}

    def provider_of(self, model_id: str) -> str | None:
        return next((m.provider for m in self.models if m.id == model_id), None)

    def models_json(self) -> list[dict[str, str]]:
        return [{"id": m.id, "provider": m.provider} for m in self.models]

    def capabilities_json(self) -> list[dict[str, str]]:
        return [{"capability": c.capability, "model": c.model, "provider": c.provider} for c in self.capabilities]

    @classmethod
    def from_json(
        cls,
        models: Sequence[object] | None,
        default_model: str | None,
        default_provider: str | None,
        capabilities: Sequence[object] | None = None,
    ) -> ModelCatalog:
        parsed = {
            CatalogModel(str(entry["id"]), str(entry.get("provider") or ""))
            for entry in models or ()
            if isinstance(entry, dict) and entry.get("id")
        }
        by_capability: dict[str, CapabilityModel] = {}
        for entry in capabilities or ():
            if isinstance(entry, dict) and entry.get("capability") and entry.get("model"):
                capability = str(entry["capability"])
                by_capability.setdefault(
                    capability, CapabilityModel(capability, str(entry["model"]), str(entry.get("provider") or ""))
                )
        return cls(
            tuple(sorted(parsed)),
            default_model or None,
            default_provider or None,
            tuple(sorted(by_capability.values())),
        )


def _catalog_after(row: ModelCatalogAnnouncement) -> ModelCatalog:
    return ModelCatalog.from_json(row.models, row.default_model, row.default_provider, row.capabilities)


def _catalog_before(row: ModelCatalogAnnouncement) -> ModelCatalog:
    return ModelCatalog.from_json(row.previous_models, row.previous_default_model, None, row.previous_capabilities)


def _known_catalog(row: ModelCatalogAnnouncement, before: ModelCatalog, after: ModelCatalog) -> ModelCatalog:
    """The catalog ``row`` recorded.

    A row recorded before the capabilities were tracked says nothing of them:
    the ones ``before`` (the boot's pre-seed read) and ``after`` agree on are
    what the platform already had. One whose stored model the boot replaced
    may never have been pinned (the provider fill-in stores the first built-in
    model, a retired one included), so it is announced as newly available,
    never as replacing it.
    """
    known = _catalog_after(row)
    if row.capabilities is not None:
        return known
    current = {(c.capability, c.model) for c in after.capabilities}
    return replace(known, capabilities=tuple(c for c in before.capabilities if (c.capability, c.model) in current))


async def read_model_catalog(db: AsyncSession) -> ModelCatalog:
    """The env-managed catalog users pick from, as the database holds it now.

    Restricted providers are left out, as everyone reads the email.
    """
    from app.api.v1.platform_settings_helpers import peek_platform_settings
    from app.llm.router import is_chat_capable_model
    from app.models.llm_config import LLMProviderConfig

    rows = (
        await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.user_id.is_(None),
                LLMProviderConfig.is_from_env.is_(True),
                LLMProviderConfig.access_restricted.is_(False),
            )
        )
    ).scalars()
    models: set[CatalogModel] = set()
    for row in rows:
        offered = set(row.available_models or [])
        for model_id in row.env_models or []:
            if model_id in offered and is_chat_capable_model(model_id):
                models.add(CatalogModel(model_id, row.provider))
    default_model = default_provider = None
    ps = await peek_platform_settings(db)
    if (
        settings.default_model.strip()
        and ps is not None
        and ps.agent_default_model
        and is_chat_capable_model(ps.agent_default_model)
    ):
        default_model = ps.agent_default_model
        default_provider = ps.agent_default_provider
    capabilities = await _pinned_capabilities(db, ps) if ps is not None else ()
    return ModelCatalog(tuple(sorted(models)), default_model, default_provider, capabilities)


async def _pinned_capabilities(db: AsyncSession, ps) -> tuple[CapabilityModel, ...]:
    """The capability models the environment pins, named with their provider's type.

    A capability the users cannot use is left out (``capability_offered``: the
    video and music models while the Video Studio is off), so it is
    never announced, and turning its feature on announces it as new.
    """
    from app.llm.env_capability_defaults import capability_offered, pinned_choices
    from app.models.llm_config import LLMProviderConfig

    choices = [choice for choice in pinned_choices(ps) if capability_offered(choice.key)]
    if not choices:
        return ()
    configs = {
        config_id: (provider, restricted)
        for config_id, provider, restricted in (
            await db.execute(
                select(LLMProviderConfig.id, LLMProviderConfig.provider, LLMProviderConfig.access_restricted)
            )
        ).all()
    }
    found: list[CapabilityModel] = []
    for choice in choices:
        provider, restricted = configs.get(choice.provider_id, (choice.provider or "", False))
        if not restricted:
            found.append(CapabilityModel(choice.key, choice.model, provider))
    return tuple(sorted(found))


# --- The change --------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class CapabilityNews:
    """A capability the users can now use, or whose model changed."""

    capability: str
    model: str
    provider: str
    # The model it ran on before; ``None`` when the capability is new.
    previous_model: str | None = None


@dataclass(frozen=True)
class CatalogChange:
    """What an email says about a move from one catalog to another."""

    # The model the default moved to, when it moved.
    new_default: CatalogModel | None
    previous_default: str | None
    # Newly offered models, the new default excluded (it has its own card).
    added: tuple[CatalogModel, ...]
    removed: tuple[CatalogModel, ...]
    # The new default was not offered before: one more new model.
    default_newly_offered: bool = False
    capabilities: tuple[CapabilityNews, ...] = ()

    @property
    def announces(self) -> bool:
        return self.new_default is not None or bool(self.added) or bool(self.removed) or bool(self.capabilities)

    @property
    def new_model_count(self) -> int:
        return len(self.added) + (1 if self.new_default is not None and self.default_newly_offered else 0)

    def describe(self) -> str:
        """One line for the logs: ``default a -> b; new: c, d; retired: e``."""
        parts = []
        if self.new_default is not None:
            parts.append(f"default {self.previous_default or 'none'} -> {self.new_default.id}")
        if self.added:
            parts.append("new: " + ", ".join(m.id for m in self.added))
        if self.removed:
            parts.append("retired: " + ", ".join(m.id for m in self.removed))
        parts.extend(f"{c.capability} {c.previous_model or 'none'} -> {c.model}" for c in self.capabilities)
        return "; ".join(parts) or "nothing users pick from"


def diff_catalogs(before: ModelCatalog, after: ModelCatalog) -> CatalogChange:
    new_default: CatalogModel | None = None
    if after.default_model and after.default_model != before.default_model:
        new_default = CatalogModel(
            after.default_model,
            after.default_provider or after.provider_of(after.default_model) or "",
        )
    before_ids, after_ids = before.ids(), after.ids()
    added = tuple(m for m in after.models if m.id not in before_ids and (new_default is None or m.id != new_default.id))
    # A capability no longer pinned is not news: another choice takes over.
    earlier = {c.capability: c.model for c in before.capabilities}
    capabilities = tuple(
        CapabilityNews(c.capability, c.model, c.provider, earlier.get(c.capability))
        for c in after.capabilities
        if earlier.get(c.capability) != c.model
    )
    return CatalogChange(
        new_default=new_default,
        previous_default=before.default_model if new_default else None,
        added=added,
        removed=tuple(m for m in before.models if m.id not in after_ids),
        default_newly_offered=new_default is not None and new_default.id not in before_ids,
        capabilities=capabilities,
    )


# --- Recording, at the env seed --------------------------------------------------


def _initial_status(change: CatalogChange) -> tuple[str, str | None]:
    from app.services.email import is_email_configured

    if not change.announces:
        return "recorded", None
    if settings.model_announcement_emails_disabled:
        return "skipped", "turned_off"
    if not settings.model_catalog_emails_enabled:
        return "skipped", "disabled"
    if not is_email_configured():
        return "skipped", "smtp_not_configured"
    return "pending", None


def _settle_hold(now: datetime, current: datetime | None = None) -> datetime | None:
    """When a queued env change may go out: once the env catalog stayed unchanged
    for ``MODEL_CATALOG_EMAILS_SETTLE_MINUTES``, and never before the back-off
    ``current`` already sets."""
    minutes = settings.model_catalog_emails_settle_minutes
    hold = now + timedelta(minutes=minutes) if minutes > 0 else None
    if current is None or hold is None:
        return hold or current
    return max(current, hold)


def _new_row(
    *,
    created_at: datetime,
    before: ModelCatalog | None,
    after: ModelCatalog,
    status: str,
    skip_reason: str | None = None,
) -> ModelCatalogAnnouncement:
    return ModelCatalogAnnouncement(
        id=uuid.uuid4(),
        created_at=created_at,
        source=SOURCE_ENV,
        previous_models=before.models_json() if before is not None else None,
        previous_default_model=before.default_model if before is not None else None,
        previous_capabilities=before.capabilities_json() if before is not None else None,
        models=after.models_json(),
        default_model=after.default_model,
        default_provider=after.default_provider,
        capabilities=after.capabilities_json(),
        status=status,
        skip_reason=skip_reason,
        failed_attempts=0,
    )


async def _has_recipients(db: AsyncSession, announcement_id: uuid.UUID) -> bool:
    # A deferred address was told nothing yet.
    return bool(
        await db.scalar(
            select(
                exists().where(
                    ModelCatalogAnnouncementRecipient.announcement_id == announcement_id,
                    ModelCatalogAnnouncementRecipient.status != "deferred",
                )
            )
        )
    )


_CONFIGURATION_SKIPS = ("disabled", "smtp_not_configured", "turned_off")


async def _told_nobody(db: AsyncSession, row: ModelCatalogAnnouncement, now: datetime) -> bool:
    """``row`` is an env change the users were never told about and may still be.

    A pending row nobody was emailed yet, or one skipped for the configuration
    alone (the emails off, no SMTP) that is younger than ``MAX_AGE`` and was not
    announced by an admin since.
    """
    if row.status == "pending":
        return not await _has_recipients(db, row.id)
    if row.status != "skipped" or row.skip_reason not in _CONFIGURATION_SKIPS or row.created_at <= now - MAX_AGE:
        return False
    if await _has_recipients(db, row.id):
        return False
    announced_since = await db.scalar(
        select(
            exists().where(
                ModelCatalogAnnouncement.source == SOURCE_ADMIN,
                # A default announced from Settings; an admin catalog change says nothing of the env's.
                ModelCatalogAnnouncement.default_model.is_not(None),
                ModelCatalogAnnouncement.created_at > row.created_at,
                ModelCatalogAnnouncement.status.in_(("pending", "sending", "sent")),
            )
        )
    )
    return not announced_since


async def record_catalog_change(
    db: AsyncSession,
    before_live: ModelCatalog,
    after_live: ModelCatalog,
    *,
    now: datetime | None = None,
) -> ModelCatalogAnnouncement | None:
    """Record the catalog the seed left; return the row that now describes it.

    ``None`` when nothing moved. The caller must hold ``bootstrap_lock()``: two
    concurrent records would both compare against the same latest row.
    """
    now = now or datetime.now(UTC)
    # Locked: a scheduler tick claiming it meanwhile would otherwise send the
    # diff this record is about to rewrite. Admin rows carry no catalog.
    latest = (
        await db.execute(
            select(ModelCatalogAnnouncement)
            .where(ModelCatalogAnnouncement.source == SOURCE_ENV)
            .order_by(ModelCatalogAnnouncement.created_at.desc(), ModelCatalogAnnouncement.id.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if latest is None:
        if before_live.is_empty and after_live.is_empty:
            return None
        latest = _new_row(created_at=now, before=None, after=before_live, status="baseline")
        db.add(latest)
        # Strictly after the baseline, so "latest" is never a tie.
        now += timedelta(microseconds=1)
    known = _known_catalog(latest, before_live, after_live)

    if after_live == known:
        await db.commit()
        return None

    if known.is_empty:
        row = _new_row(created_at=now, before=known, after=after_live, status="recorded")
        db.add(row)
        await db.commit()
        return row

    if await _told_nobody(db, latest, now):
        # Nobody was told about the previous change yet: tell them about the
        # net change instead, or about nothing when it cancelled out.
        origin = _catalog_before(latest)
        if latest.previous_capabilities is None:
            origin = replace(origin, capabilities=known.capabilities)
            latest.previous_capabilities = origin.capabilities_json()
        latest.models = after_live.models_json()
        latest.default_model = after_live.default_model
        latest.default_provider = after_live.default_provider
        latest.capabilities = after_live.capabilities_json()
        net = diff_catalogs(origin, after_live)
        was_pending = latest.status == "pending"
        latest.status, latest.skip_reason = _initial_status(net) if net.announces else ("superseded", None)
        if latest.status != "pending" or not was_pending:
            latest.next_attempt_at, latest.failed_attempts, latest.last_error = None, 0, None
        if latest.status == "pending":
            # The catalog moved again: the wait starts over.
            latest.next_attempt_at = _settle_hold(now, latest.next_attempt_at)
        await db.commit()
        return latest

    status, reason = _initial_status(diff_catalogs(known, after_live))
    row = _new_row(created_at=now, before=known, after=after_live, status=status, skip_reason=reason)
    if status == "pending":
        row.next_attempt_at = _settle_hold(now)
    db.add(row)
    await db.commit()
    return row


@asynccontextmanager
async def model_catalog_watch() -> AsyncIterator[None]:
    """Record the catalog change the wrapped env seed makes. Never raises into its caller."""
    before: ModelCatalog | None = None
    try:
        async with async_session() as db:
            before = await read_model_catalog(db)
    except Exception as exc:
        logger.warning(
            "Model catalog announcements: could not read the catalog before the env seed "
            "(the change is measured against the last recorded catalog only): %s",
            format_exception_message(exc),
        )
    yield
    after: ModelCatalog | None = None
    try:
        async with async_session() as db:
            after = await read_model_catalog(db)
            # Without the pre-seed read, the latest recorded row still says what
            # the platform knew; only a first boot has nothing to compare with.
            row = await record_catalog_change(db, before if before is not None else after, after)
            if row is None:
                logger.info("Model catalog announcements: the env catalog is unchanged, nothing to announce")
            else:
                await _log_recorded(db, row)
    except Exception as exc:
        logger.warning(
            "Model catalog announcements: could not record the catalog change: %s",
            format_exception_message(exc),
        )
    try:
        async with async_session() as db:
            await apply_boot_configuration(db, after)
    except Exception as exc:
        logger.warning(
            "Model catalog announcements: could not settle the queued announcements for this boot: %s",
            format_exception_message(exc),
        )


_SKIP_EXPLANATIONS = {
    "disabled": "MODEL_CATALOG_EMAILS_ENABLED is off",
    "smtp_not_configured": "SMTP is not configured (SMTP_HOSTNAME)",
    "turned_off": "MODEL_ANNOUNCEMENT_EMAILS_DISABLED is on",
}


def _when_sent(row: ModelCatalogAnnouncement) -> str:
    """When the queued ``row`` goes out, as the logs say it."""
    if row.status == "pending" and row.next_attempt_at is not None:
        return (
            f"from {row.next_attempt_at.isoformat(timespec='seconds')}, if the env catalog does not change "
            "before then (a change before then joins this email)"
        )
    return "from the next scheduler tick"


async def _log_recorded(db: AsyncSession, row: ModelCatalogAnnouncement) -> None:
    change = announcement_change(row)
    if row.status in ("pending", "sending"):
        logger.warning(
            "Model catalog changed by the env seed (%s): emailing %d user(s) %s (announcement %s)",
            change.describe(),
            await _count_eligible(db, SOURCE_ENV),
            _when_sent(row),
            row.id,
        )
    elif row.status == "skipped":
        logger.warning(
            "Model catalog changed by the env seed (%s): the users are not emailed, %s (announcement %s)",
            change.describe(),
            _SKIP_EXPLANATIONS.get(row.skip_reason or "", row.skip_reason),
            row.id,
        )
    elif row.status == "superseded":
        logger.warning(
            "Model catalog changed back by the env seed before anyone was emailed: announcement %s dropped",
            row.id,
        )
    else:
        logger.info(
            "Model catalog changed by the env seed (%s): nothing to announce (%s)", change.describe(), row.status
        )


async def _close_open_rows(
    db: AsyncSession, *conditions, status: str, skip_reason: str | None, now: datetime
) -> list[uuid.UUID]:
    """Close the open rows matching ``conditions``; a send reserved on one is settled ``unknown``."""
    closed = list(
        (
            await db.scalars(
                update(ModelCatalogAnnouncement)
                .where(ModelCatalogAnnouncement.status.in_(_ACTIVE), *conditions)
                .values(status=status, skip_reason=skip_reason, claim_token=None, next_attempt_at=None)
                .returning(ModelCatalogAnnouncement.id)
                .execution_options(synchronize_session=False)
            )
        ).all()
    )
    if closed:
        await db.execute(
            update(ModelCatalogAnnouncementRecipient)
            .where(
                ModelCatalogAnnouncementRecipient.announcement_id.in_(closed),
                ModelCatalogAnnouncementRecipient.status == "sending",
            )
            .values(status="unknown", updated_at=now)
            .execution_options(synchronize_session=False)
        )
    return closed


async def _revive_skipped_change(
    db: AsyncSession, live: ModelCatalog, now: datetime
) -> tuple[ModelCatalogAnnouncement, str] | None:
    """Queue the latest env change again when it was skipped for the configuration alone.

    Returns the row and the reason it had been skipped.
    """
    latest = await db.scalar(
        select(ModelCatalogAnnouncement)
        .where(ModelCatalogAnnouncement.source == SOURCE_ENV)
        .order_by(ModelCatalogAnnouncement.created_at.desc(), ModelCatalogAnnouncement.id.desc())
        .limit(1)
        .with_for_update()
    )
    if (
        latest is None
        or latest.status != "skipped"
        or _known_catalog(latest, live, live) != live
        or not announcement_change(latest).announces
        or not await _told_nobody(db, latest, now)
    ):
        return None
    reason = latest.skip_reason or ""
    latest.status, latest.skip_reason = "pending", None
    latest.claim_token, latest.next_attempt_at = None, _settle_hold(now)
    latest.failed_attempts, latest.last_error = 0, None
    return latest, reason


async def apply_boot_configuration(db: AsyncSession, live: ModelCatalog | None, *, now: datetime | None = None) -> None:
    """Settle the open announcements for the configuration this boot runs with.

    ``live`` is the catalog the seed left (``None`` when it could not be read:
    nothing is revived then). The caller must hold ``bootstrap_lock()``.
    """
    from app.services.email import is_email_configured

    now = now or datetime.now(UTC)
    if settings.model_announcement_emails_disabled:
        closed = await _close_open_rows(db, status="skipped", skip_reason="turned_off", now=now)
        await db.commit()
        if closed:
            logger.warning(
                "Model catalog announcements: %d queued announcement(s) closed unsent, %s",
                len(closed),
                _SKIP_EXPLANATIONS["turned_off"],
            )
        return
    if not is_email_configured():
        closed = await _close_open_rows(db, status="skipped", skip_reason="smtp_not_configured", now=now)
        await db.commit()
        if closed:
            logger.warning(
                "Model catalog announcements: %d queued announcement(s) closed unsent, %s",
                len(closed),
                _SKIP_EXPLANATIONS["smtp_not_configured"],
            )
        return
    if not settings.model_catalog_emails_enabled:
        closed = await _close_open_rows(
            db, ModelCatalogAnnouncement.source == SOURCE_ENV, status="skipped", skip_reason="disabled", now=now
        )
        await db.commit()
        if closed:
            logger.warning(
                "Model catalog announcements: %d queued announcement(s) closed unsent, %s",
                len(closed),
                _SKIP_EXPLANATIONS["disabled"],
            )
        return
    revived = await _revive_skipped_change(db, live, now) if live is not None else None
    await db.commit()
    if revived is not None:
        row, reason = revived
        logger.warning(
            "Model catalog announcements: the change skipped at an earlier boot (%s) is emailed now (%s): "
            "%d user(s) %s (announcement %s)",
            _SKIP_EXPLANATIONS.get(reason, reason),
            announcement_change(row).describe(),
            await _count_eligible(db, SOURCE_ENV),
            _when_sent(row),
            row.id,
        )


# --- The email ---------------------------------------------------------------------

_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]+(?:-[a-z]+)?\.)?(?:anthropic|amazon|meta|mistral|cohere)\.")
_VERSION_SUFFIX = re.compile(r"(?:-\d{8})?(?:-v\d+(?::\d+)?)?(?:-latest)?$")
_CLAUDE = re.compile(r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?$")
_CLAUDE_LEGACY = re.compile(r"^claude-(?P<major>\d)(?:-(?P<minor>\d))?-(?P<family>[a-z]+)$")
_VEO = re.compile(r"^veo-(?P<version>\d+(?:\.\d+)?)(?P<fast>-fast)?-generate(?:-[a-z0-9]+)*$")
_LYRIA = re.compile(r"^lyria-(?P<version>\d+(?:\.\d+)?)(?P<clip>-clip)?(?:-preview)?$")
_OPENAI_TTS = re.compile(r"^tts-(?P<version>\d+)(?P<hd>-hd)?$")


def _word(part: str, *, title: bool = False) -> str:
    """One word of a model id, as a name: ``tts`` is an acronym."""
    if part == "tts":
        return "TTS"
    return part.title() if title and not part[:1].isdigit() else part


def model_display_name(model_id: str | None) -> str:
    """``claude-sonnet-5-5`` -> ``Claude Sonnet 5.5``; an unknown shape is kept as is."""
    raw = (model_id or "").strip()
    base = _VERSION_SUFFIX.sub("", _BEDROCK_PREFIX.sub("", raw.lower()))
    if match := _CLAUDE.match(base):
        version = match["major"] + (f".{match['minor']}" if match["minor"] else "")
        return f"Claude {match['family'].title()} {version}"
    if match := _CLAUDE_LEGACY.match(base):
        version = match["major"] + (f".{match['minor']}" if match["minor"] else "")
        return f"Claude {version} {match['family'].title()}"
    if base.startswith("gpt-") and len(base) > 4:
        head, *rest = base[4:].split("-")
        return "GPT-" + head + "".join(f" {_word(part)}" for part in rest)
    if base.startswith("gemini-"):
        parts = base.split("-")[1:]
        return "Gemini " + " ".join(_word(p, title=True) for p in parts)
    if match := _VEO.match(base):
        return f"Veo {match['version']}" + (" Fast" if match["fast"] else "")
    if match := _LYRIA.match(base):
        return f"Lyria {match['version']}" + (" Clip" if match["clip"] else "")
    if match := _OPENAI_TTS.match(base):
        return f"TTS-{match['version']}" + (" HD" if match["hd"] else "")
    return raw


def provider_display_name(provider: str | None) -> str:
    key = (provider or "").strip().lower()
    return _PROVIDER_NAMES.get(key, key.replace("_", " ").title())


def _join_names(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


@dataclass(frozen=True)
class AnnouncementEmail:
    subject: str
    html: str
    text: str


def _it(count: int) -> str:
    return "it" if count == 1 else "them"


# What each capability is called, what its model does for the users, and what
# it does in the Video Studio besides, said only while the Video Studio is on:
# an email never names a feature its readers do not have.
_CAPABILITIES = {
    "image": ("Image generation", "creates the images your agents and conversations generate", ""),
    "video": ("Video generation", "generates the AI clips of the Video Studio", ""),
    "speech": ("Speech generation", "reads answers aloud", " and narrates Video Studio videos"),
    "music": ("Music generation", "composes the AI music of the Video Studio", ""),
    "decision": ("Page actions", "lets agents act on the page of an embedded chat", ""),
}


def _capability(key: str) -> tuple[str, str]:
    from app.core.feature_flags import is_feature_enabled

    label, does, in_video_studio = _CAPABILITIES.get(
        key, (key.replace("_", " ").capitalize(), "is used by your agents", "")
    )
    return label, does + (in_video_studio if is_feature_enabled("VIDEO_STUDIO") else "")


def render_announcement(change: CatalogChange, *, recipient_name: str | None, platform_name: str) -> AnnouncementEmail:
    """The email ``change`` sends: its headline is the biggest news it carries.

    Several new models: "New models available in your <platform> platform".
    Otherwise a new default, then a single new model, then retired models, then
    the capabilities alone. Every new model is listed with its provider,
    then each capability with its model, the retired models last.
    """
    from app.services.email import (
        EmailPanelItem,
        email_button,
        email_heading,
        email_link,
        email_panel,
        email_paragraph,
        email_paragraph_html,
        email_signoff_team,
        platform_url,
        render_email,
    )

    url = platform_url()
    host = urlsplit(url).netloc or url
    name = html_escape(platform_name)
    greeting = f"Hello {recipient_name}," if recipient_name else "Hello,"
    runs_on_default = (
        "Agents and conversations that don't use a specific model run on it from now on: "
        "there is nothing you need to do."
    )
    default_label = model_display_name(change.new_default.id) if change.new_default is not None else None
    added = [model_display_name(m.id) for m in change.added]
    retired = [model_display_name(m.id) for m in change.removed]
    follow_default = f"Agents and personal preferences that used {_it(len(retired))} now follow the default model."

    if change.new_model_count >= 2:
        new = ([default_label] if default_label and change.default_newly_offered else []) + added
        title = subject = f"New models available in your {platform_name} platform"
        preheader = f"{len(new)} new models to select for your agents and conversations."
        select = "You can select them for your agents and conversations from now on."
        lead_text = f"{_join_names(new)} are now available on your {platform_name} platform. {select}"
        lead_html = (
            f"<strong>{html_escape(_join_names(new))}</strong> are now available on your {name} platform. {select}"
        )
        if default_label:
            lead_text += f" {default_label} is also the new default model. {runs_on_default}"
            lead_html += (
                f" <strong>{html_escape(default_label)}</strong> is also the new default model. {runs_on_default}"
            )
    elif default_label:
        title = f"{default_label} is now your default model"
        subject = f"{default_label} is now your default model on {platform_name}"
        preheader = f"Agents and conversations that don't use a specific model now run on {default_label}."
        lead_text = f"{default_label} is now the default model of your {platform_name} platform. {runs_on_default}"
        lead_html = (
            f"<strong>{html_escape(default_label)}</strong> is now the default model of your {name} platform. "
            f"{runs_on_default}"
        )
    elif added:
        select = "You can select it for your agents and conversations from now on."
        title = f"{added[0]} is now available"
        subject = f"{added[0]} is now available on {platform_name}"
        preheader = "Select it for your agents and conversations."
        lead_text = f"{added[0]} is now available on your {platform_name} platform. {select}"
        lead_html = f"<strong>{html_escape(added[0])}</strong> is now available on your {name} platform. {select}"
    elif retired:
        one = len(retired) == 1
        title = f"{retired[0]} is no longer available" if one else f"Models retired from your {platform_name} platform"
        subject = f"{retired[0]} is no longer available on {platform_name}" if one else title
        preheader = follow_default
        verb = "is" if one else "are"
        lead_text = (
            f"{_join_names(retired)} {verb} no longer available on your {platform_name} platform. {follow_default}"
        )
        lead_html = (
            f"<strong>{html_escape(_join_names(retired))}</strong> {verb} no longer available on your "
            f"{name} platform. {follow_default}"
        )
    elif len(change.capabilities) == 1:
        news_item = change.capabilities[0]
        label, does = _capability(news_item.capability)
        model = model_display_name(news_item.model)
        if news_item.previous_model is None:
            title = f"{label} is now available"
            subject = f"{label} is now available on {platform_name}"
            lead_text = f"{label} is now available on your {platform_name} platform: {model} {does}."
            lead_html = (
                f"{html_escape(label)} is now available on your {name} platform: "
                f"<strong>{html_escape(model)}</strong> {does}."
            )
        else:
            previous = model_display_name(news_item.previous_model)
            title = f"{label} now uses {model}"
            subject = f"{label} now uses {model} on {platform_name}"
            lead_text = f"{model} now {does} on your {platform_name} platform, in place of {previous}."
            lead_html = (
                f"<strong>{html_escape(model)}</strong> now {does} on your {name} platform, "
                f"in place of {html_escape(previous)}."
            )
        preheader = f"{model} {does}."
    else:
        labels = [_capability(c.capability)[0] for c in change.capabilities]
        listed = _join_names([labels[0]] + [label.lower() for label in labels[1:]])
        title = subject = f"New AI capabilities in your {platform_name} platform"
        preheader = f"{listed}: what your agents can do now."
        lead_text = f"{listed} are ready on your {platform_name} platform, with the models below."
        lead_html = f"<strong>{html_escape(listed)}</strong> are ready on your {name} platform, with the models below."

    model_news = change.new_default is not None or bool(change.added) or bool(change.removed)
    if model_news and change.capabilities:
        also_on = _join_names(
            [f"{_capability(c.capability)[0].lower()} with {model_display_name(c.model)}" for c in change.capabilities]
        )
        lead_text += f" Also on your platform: {also_on}."
        lead_html += f" Also on your platform: {html_escape(also_on)}."

    news: list[EmailPanelItem] = []
    if change.new_default is not None:
        previous = model_display_name(change.previous_default) if change.previous_default else None
        news.append(
            EmailPanelItem(
                eyebrow="Default model",
                title=default_label or change.new_default.id,
                detail=provider_display_name(change.new_default.provider) or None,
                code=change.new_default.id,
                note=f"Replaces {previous}" if previous else None,
            )
        )
    also = change.new_default is not None and change.new_model_count < 2
    news.extend(
        EmailPanelItem(
            eyebrow="Also available" if also else "New",
            title=model_display_name(model.id),
            detail=provider_display_name(model.provider) or None,
            code=model.id,
        )
        for model in change.added
    )
    news.extend(
        EmailPanelItem(
            eyebrow=_capability(item.capability)[0],
            title=model_display_name(item.model),
            detail=provider_display_name(item.provider) or None,
            code=item.model,
            note=f"Replaces {model_display_name(item.previous_model)}" if item.previous_model else None,
        )
        for item in change.capabilities
    )
    gone = [
        EmailPanelItem(
            eyebrow="Retired",
            title=model_display_name(model.id),
            detail=provider_display_name(model.provider) or None,
            code=model.id,
            muted=True,
        )
        for model in change.removed
    ]
    # Said in the lead when retirements are the headline.
    retired_note = follow_default if gone and (change.new_default is not None or change.added) else ""

    body = (
        email_heading(title)
        + email_paragraph(greeting)
        + email_paragraph_html(lead_html)
        + (email_panel(news) if news else "")
        + (email_panel(gone) if gone else "")
        + (email_paragraph(retired_note, small=True, muted=True) if retired_note else "")
        + email_button(f"Open {platform_name}", url)
    )
    fine_print = (
        f"You are receiving this automated email because you have an account on {name} "
        f"({email_link(host, url)}). If you have any questions about this change, contact "
        "your platform administrator."
    )
    html = render_email(title=title, preheader=preheader, body_html=body, fine_print_html=fine_print)

    lines = [greeting, "", lead_text, ""]
    for group in (news, gone):
        for item in group:
            detail = " - ".join(part for part in (item.detail, item.code) if part)
            lines.append(f"{item.eyebrow}: {item.title}" + (f" ({detail})" if detail else ""))
            if item.note:
                lines.append(f"  {item.note}")
        if group:
            lines.append("")
    if retired_note:
        lines += [retired_note, ""]
    lines += [
        f"Open {platform_name}: {url}",
        "",
        "Best regards,",
        email_signoff_team(),
        "",
        f"You are receiving this automated email because you have an account on {platform_name} "
        f"({host}). If you have any questions about this change, contact your platform administrator.",
    ]
    return AnnouncementEmail(subject=subject, html=html, text="\n".join(lines))


# --- Delivery, from the scheduler ------------------------------------------------

_ACTIVE = ("pending", "sending")


@dataclass(frozen=True)
class _Recipient:
    id: uuid.UUID
    email: str
    full_name: str | None
    # The retry of an address the server refused for now.
    retry: bool = False


SendEmail = Callable[..., Awaitable[None]]


def _backoff(failed_attempts: int) -> timedelta:
    return min(_BACKOFF_BASE * (2 ** max(0, failed_attempts - 1)), _BACKOFF_MAX)


def _eligible_users(source: str) -> list:
    """Who an announcement is for, as read at the moment each email is sent."""
    from app.models.user import User

    conditions = [
        User.is_active.is_(True),
        User.email.contains("@"),
        # The bootstrap admin is the operator's credential (on SaaS,
        # ``admin@<instance host>``), not a mailbox of someone using the platform.
        func.lower(User.email) != settings.admin_email.strip().lower(),
    ]
    # The operator's audience setting governs what the env seed announces; an
    # admin who chose to email "every user" from Settings meant every user.
    if source == SOURCE_ENV and settings.model_catalog_emails_recipients == "admins":
        conditions.append(User.role == "admin")
    return conditions


async def _count_eligible(db: AsyncSession, source: str) -> int:
    from app.models.user import User

    return int(await db.scalar(select(func.count(User.id)).where(*_eligible_users(source))) or 0)


def _not_served(announcement_id: uuid.UUID):
    from app.models.user import User

    return ~exists().where(
        ModelCatalogAnnouncementRecipient.announcement_id == announcement_id,
        ModelCatalogAnnouncementRecipient.user_id == User.id,
    )


def _deferred(announcement_id: uuid.UUID):
    """The users whose address the server refused for now, still to retry."""
    from app.models.user import User

    return exists().where(
        ModelCatalogAnnouncementRecipient.announcement_id == announcement_id,
        ModelCatalogAnnouncementRecipient.user_id == User.id,
        ModelCatalogAnnouncementRecipient.status == "deferred",
    )


async def _refusals_since_last_email(db: AsyncSession, announcement_id: uuid.UUID) -> int | None:
    """The addresses the server refused for now since it last took an email of
    this announcement; ``None`` when it took none yet."""
    recipient = ModelCatalogAnnouncementRecipient
    last_delivered = await db.scalar(
        select(func.max(recipient.updated_at)).where(
            recipient.announcement_id == announcement_id, recipient.status == "delivered"
        )
    )
    if last_delivered is None:
        return None
    return int(
        await db.scalar(
            select(func.count())
            .select_from(recipient)
            .where(
                recipient.announcement_id == announcement_id,
                recipient.status == "deferred",
                recipient.updated_at > last_delivered,
            )
        )
        or 0
    )


def _claim_expired(now: datetime):
    """A row nobody is sending: ``pending``, or ``sending`` past its lease."""
    return or_(
        ModelCatalogAnnouncement.status == "pending",
        ModelCatalogAnnouncement.next_attempt_at.is_(None),
        ModelCatalogAnnouncement.next_attempt_at <= now,
    )


async def _expire_stale(now: datetime) -> None:
    """Close the rows too old to go out, in a transaction of their own.

    Nothing else is closed here: whether a change may go out is the boot's
    decision (:func:`apply_boot_configuration`), and this tick may run in a
    process started before the configuration changed.
    """
    async with async_session() as db:
        expired = await _close_open_rows(
            db,
            _claim_expired(now),
            ModelCatalogAnnouncement.created_at < now - MAX_AGE,
            status="expired",
            skip_reason=None,
            now=now,
        )
        await db.commit()
    for row_id in expired:
        logger.warning(
            "Model catalog announcement %s expired unsent: not delivered within %d days of the change",
            row_id,
            MAX_AGE.days,
        )


# The monotonic time this process last said it cannot send (no SMTP).
_smtp_missing_reported_at: float | None = None
_SMTP_MISSING_REPORT_EVERY = 3600.0


async def _report_smtp_missing() -> None:
    """Say, at most hourly, that this process leaves the queued announcements to another."""
    global _smtp_missing_reported_at
    if (
        _smtp_missing_reported_at is not None
        and time.monotonic() - _smtp_missing_reported_at < _SMTP_MISSING_REPORT_EVERY
    ):
        return
    async with async_session() as db:
        queued = await db.scalar(
            select(func.count(ModelCatalogAnnouncement.id)).where(ModelCatalogAnnouncement.status.in_(_ACTIVE))
        )
    if queued:
        _smtp_missing_reported_at = time.monotonic()
        logger.warning(
            "Model catalog announcements: %d queued announcement(s) left to another process, "
            "SMTP is not configured in this one (SMTP_HOSTNAME)",
            queued,
        )


async def _reserve(
    db: AsyncSession,
    announcement_id: uuid.UUID,
    source: str,
    token: uuid.UUID,
    recipient: _Recipient,
    at: datetime,
) -> bool | None:
    """Reserve one recipient before its email is sent.

    ``None`` when this worker no longer holds the claim (its lease ran out and
    the row was settled or claimed again): it must stop. ``False`` when the
    user is no longer eligible or was already served (a retry: no longer
    deferred). Renews the lease.
    """
    from app.models.user import User

    user_id = recipient.id
    renewed = await db.scalar(
        update(ModelCatalogAnnouncement)
        .where(
            ModelCatalogAnnouncement.id == announcement_id,
            ModelCatalogAnnouncement.claim_token == token,
            ModelCatalogAnnouncement.status == "sending",
        )
        .values(next_attempt_at=at + CLAIM_LEASE)
        .returning(ModelCatalogAnnouncement.id)
        .execution_options(synchronize_session=False)
    )
    if renewed is None:
        await db.rollback()
        return None
    eligible = await db.scalar(select(User.id).where(User.id == user_id, *_eligible_users(source)))
    reserved = None
    if eligible is not None:
        table = ModelCatalogAnnouncementRecipient.__table__
        if recipient.retry:
            reserved = await db.scalar(
                update(table)
                .where(
                    table.c.announcement_id == announcement_id,
                    table.c.user_id == user_id,
                    table.c.status == "deferred",
                )
                .values(status="sending", updated_at=at)
                .returning(table.c.user_id)
            )
        else:
            reserved = await db.scalar(
                pg_insert(table)
                .values(announcement_id=announcement_id, user_id=user_id, status="sending", updated_at=at)
                .on_conflict_do_nothing(index_elements=[table.c.announcement_id, table.c.user_id])
                .returning(table.c.user_id)
            )
    await db.commit()
    return reserved is not None


async def _set_outcome(
    db: AsyncSession, announcement_id: uuid.UUID, user_id: uuid.UUID, status: str | None, at: datetime
) -> None:
    """Record what the send did; ``None`` releases the reservation (nothing was sent)."""
    recipient = and_(
        ModelCatalogAnnouncementRecipient.announcement_id == announcement_id,
        ModelCatalogAnnouncementRecipient.user_id == user_id,
    )
    if status is None:
        statement = delete(ModelCatalogAnnouncementRecipient).where(recipient)
    else:
        statement = update(ModelCatalogAnnouncementRecipient).where(recipient).values(status=status, updated_at=at)
    await db.execute(statement.execution_options(synchronize_session=False))
    await db.commit()


async def _settle_reserved(db: AsyncSession, announcement_id: uuid.UUID, at: datetime) -> None:
    """Settle ``unknown`` the sends a previous claimant reserved and never settled."""
    await db.execute(
        update(ModelCatalogAnnouncementRecipient)
        .where(
            ModelCatalogAnnouncementRecipient.announcement_id == announcement_id,
            ModelCatalogAnnouncementRecipient.status == "sending",
        )
        .values(status="unknown", updated_at=at)
        .execution_options(synchronize_session=False)
    )


async def _current_default_model(db: AsyncSession) -> str | None:
    from app.api.v1.platform_settings_helpers import peek_platform_settings

    ps = await peek_platform_settings(db)
    return ps.agent_default_model if ps is not None else None


async def _claim(now: datetime) -> tuple[uuid.UUID, str, uuid.UUID, CatalogChange, list[_Recipient]] | None:
    """Claim the oldest open announcement, if nobody is sending it and it is due.

    Announcements go out in the order they were recorded: a newer one waits
    while an older one is being sent or backing off.
    """
    from app.models.user import User

    async with async_session() as db:
        oldest = await db.scalar(
            select(ModelCatalogAnnouncement.id)
            .where(ModelCatalogAnnouncement.status.in_(_ACTIVE))
            .order_by(ModelCatalogAnnouncement.created_at, ModelCatalogAnnouncement.id)
            .limit(1)
        )
        if oldest is None:
            return None
        row = await db.scalar(
            select(ModelCatalogAnnouncement)
            .where(ModelCatalogAnnouncement.id == oldest)
            .with_for_update(skip_locked=True)
        )
        if row is None or row.status not in _ACTIVE or (row.next_attempt_at is not None and row.next_attempt_at > now):
            return None
        # Sends a previous claimant reserved and never settled (it died, or its
        # lease ran out mid-send): whether they went out is unknown, and they
        # are never sent again.
        await _settle_reserved(db, row.id, now)
        if row.source == SOURCE_ADMIN and (stale := await _stale_admin_news(db, row)) is not None:
            row.status, row.claim_token, row.next_attempt_at = "superseded", None, None
            await db.commit()
            logger.warning("Model catalog announcement %s dropped unsent: %s", row.id, stale)
            return None
        token = uuid.uuid4()
        row.status = "sending"
        row.claim_token = token
        row.next_attempt_at = now + CLAIM_LEASE
        change = diff_catalogs(_catalog_before(row), _catalog_after(row))
        batch: list[_Recipient] = []
        # Everyone not emailed yet first; the deferred addresses once nobody else is left.
        for waiting, retry in ((_not_served(row.id), False), (_deferred(row.id), True)):
            batch = [
                _Recipient(user_id, email, full_name, retry=retry)
                for user_id, email, full_name in (
                    await db.execute(
                        select(User.id, User.email, User.full_name)
                        .where(*_eligible_users(row.source), waiting)
                        .order_by(User.id)
                        .limit(settings.model_catalog_emails_batch_size)
                    )
                ).all()
            ]
            if batch:
                break
        row_id, source = row.id, row.source
        await db.commit()
    return row_id, source, token, change, batch


async def _finish(
    row_id: uuid.UUID,
    token: uuid.UUID,
    *,
    sent: int,
    error: str | None,
    deferral: str | None = None,
    deferral_counted: bool = False,
    retry_pass: bool = False,
    at: datetime,
) -> None:
    """Settle the claim: ``sent`` emails went out in this batch.

    ``deferral`` is the server's last reply to an address it refused for now,
    ``deferral_counted`` whether one of those refusals may have been the
    server refusing the platform (counted in the run that stops a batch);
    ``retry_pass`` the batch retried such addresses.
    """
    from app.models.user import User

    async with async_session() as db:
        row = await db.get(ModelCatalogAnnouncement, row_id, with_for_update=True)
        if row is None or row.status != "sending" or row.claim_token != token:
            # The lease ran out mid-batch: the row belongs to whoever settled or
            # claimed it since.
            await db.commit()
            logger.warning("Model catalog announcement %s: claim lost before the batch ended", row_id)
            return
        row.claim_token = None
        if error is not None:
            # Consecutive failed batches; one that got an email out first
            # starts the count again. Addresses set aside do not: a server
            # refusing every address refuses the platform, and the back-off
            # must grow as for any other failure.
            row.failed_attempts = 1 if sent else (row.failed_attempts or 0) + 1
            row.last_error = error[:2000]
            if row.failed_attempts >= MAX_ATTEMPTS:
                row.status, row.next_attempt_at = "failed", None
                logger.error(
                    "Model catalog announcement %s: giving up after %d failed batches: %s",
                    row_id,
                    row.failed_attempts,
                    error,
                )
            else:
                row.status, row.next_attempt_at = "pending", at + _backoff(row.failed_attempts)
                logger.warning(
                    "Model catalog announcement %s: SMTP failure, retrying at %s: %s",
                    row_id,
                    row.next_attempt_at.isoformat(),
                    error,
                )
            await db.commit()
            return
        failures_before = row.failed_attempts or 0
        row.failed_attempts = 0
        if deferral is not None:
            # The status shows it as the last address refused (``last_refusal``),
            # a refusal that counted toward the run included when an email went
            # out in this batch: the server takes the platform's mail. Only a
            # batch that got none out past a counted refusal reports it as an
            # email server error, the failed batch below.
            row.last_error = deferral[:2000]
        elif failures_before:
            # The failure that held the batches is over; the error went with it.
            row.last_error = None
        remaining = await db.scalar(
            select(func.count(User.id)).where(*_eligible_users(row.source), _not_served(row_id))
        )
        if remaining:
            row.status, row.next_attempt_at = "pending", None
            if deferral_counted and not sent:
                # Every address tried was refused for now, in a batch too small
                # for the run that stops one, and one refusal may have been the
                # server's: wait as after a failed batch.
                row.failed_attempts = failures_before + 1
                row.next_attempt_at = at + _backoff(row.failed_attempts)
            await db.commit()
            return
        deferred = await db.scalar(select(func.count(User.id)).where(*_eligible_users(row.source), _deferred(row_id)))
        if deferred and retry_pass:
            # The wait comes before the retries, not between their batches.
            row.status, row.next_attempt_at = "pending", None
            await db.commit()
            return
        if deferred:
            # No failed batch: everyone else was emailed. The status reports the
            # retry and the last refusal, never an email server error.
            row.status, row.next_attempt_at = "pending", at + DEFERRED_RETRY_DELAY
            await db.commit()
            logger.warning(
                "Model catalog announcement %s: %d address(es) refused for now by the email server, "
                "retried once at %s: %s",
                row_id,
                deferred,
                row.next_attempt_at.isoformat(),
                row.last_error,
            )
            return
        outcomes = dict(
            (
                await db.execute(
                    select(ModelCatalogAnnouncementRecipient.status, func.count())
                    .where(ModelCatalogAnnouncementRecipient.announcement_id == row_id)
                    .group_by(ModelCatalogAnnouncementRecipient.status)
                )
            ).all()
        )
        row.next_attempt_at = None
        if outcomes.get("delivered") or outcomes.get("unknown"):
            # The last refusal of the retries is over with them: a sent row
            # keeps no error.
            row.status, row.sent_at, row.last_error = "sent", at, None
            logger.warning(
                "Model catalog announcement %s sent to %d user(s) (%d refused, %d unknown)",
                row_id,
                outcomes.get("delivered", 0),
                outcomes.get("refused", 0),
                outcomes.get("unknown", 0),
            )
        elif outcomes.get("refused"):
            row.status, row.last_error = "failed", "Every recipient refused the email"
            logger.warning(
                "Model catalog announcement %s failed: every recipient refused the email (%d)",
                row_id,
                outcomes["refused"],
            )
        else:
            row.status, row.skip_reason = "skipped", "no_recipients"
            logger.warning(
                "Model catalog announcement %s not sent: no user to email (active users with an email address, "
                "the bootstrap admin ADMIN_EMAIL excluded%s)",
                row_id,
                ", admins only per MODEL_CATALOG_EMAILS_RECIPIENTS"
                if row.source == SOURCE_ENV and settings.model_catalog_emails_recipients == "admins"
                else "",
            )
        await db.commit()


async def deliver_pending_announcements(*, send: SendEmail | None = None, now: datetime | None = None) -> int:
    """Email the next batch of the oldest open announcement; return how many went out.

    Each recipient is reserved in ``model_catalog_announcement_recipients``
    before the send and settled after it, each step its own short transaction
    (no connection is held while SMTP runs): whatever crashes, and whichever
    worker claims the row next, a user is emailed at most once.
    """
    from app.services.branding import get_cached_branding
    from app.services.email import EmailDeliveryError, deliver_email, email_greeting_name, is_email_configured

    send = send or deliver_email
    now = now or datetime.now(UTC)
    started = time.monotonic()

    def clock() -> datetime:
        return now + timedelta(seconds=time.monotonic() - started)

    await _expire_stale(now)
    if settings.model_announcement_emails_disabled:
        # The boot closed the open rows; one queued by a process started
        # without the switch is left to it, never sent from here.
        return 0
    if not is_email_configured():
        await _report_smtp_missing()
        return 0
    claim = await _claim(now)
    if claim is None:
        return 0
    row_id, source, token, change, batch = claim

    platform_name = get_cached_branding().name
    sent = deferred_in_a_row = 0
    error: str | None = None
    deferral: str | None = None
    deferral_counted = False
    try:
        async with async_session() as db:
            refused_since = await _refusals_since_last_email(db, row_id)
            # The server took an email of this announcement: it takes the platform's mail.
            accepted = refused_since is not None
            refused_in_a_row = refused_since or 0
            for recipient in batch:
                email = render_announcement(
                    change,
                    recipient_name=email_greeting_name(recipient.full_name),
                    platform_name=platform_name,
                )
                reserved = await _reserve(db, row_id, source, token, recipient, clock())
                if reserved is None:
                    break
                if not reserved:
                    continue
                try:
                    await send(to=recipient.email, subject=email.subject, html=email.html, text=email.text)
                except EmailDeliveryError as exc:
                    if exc.permanent:
                        logger.warning(
                            "Model catalog announcement %s: recipient %s refused: %s", row_id, recipient.id, exc
                        )
                        await _set_outcome(db, row_id, recipient.id, "refused", clock())
                        continue
                    # The address's own refusal, once the server took an email
                    # of this announcement: it says nothing of the server,
                    # unless no email went out across a long run of them.
                    own_refusal = (
                        exc.recipient_refused
                        and exc.address_specific
                        and (accepted or sent > 0)
                        and (recipient.retry or refused_in_a_row < _ADDRESS_REFUSALS_IN_A_ROW)
                    )
                    if own_refusal or (exc.recipient_refused and deferred_in_a_row < _DEFERRALS_IN_A_ROW):
                        if not own_refusal:
                            deferred_in_a_row += 1
                            deferral_counted = True
                        refused_in_a_row += 1
                        deferral = str(exc)
                        logger.warning(
                            "Model catalog announcement %s: recipient %s refused for now%s: %s",
                            row_id,
                            recipient.id,
                            ", not retried again" if recipient.retry else ", retried once the others are emailed",
                            exc,
                        )
                        await _set_outcome(
                            db, row_id, recipient.id, "refused" if recipient.retry else "deferred", clock()
                        )
                        continue
                    if exc.uncertain:
                        logger.warning(
                            "Model catalog announcement %s: recipient %s may have been emailed, not retried: %s",
                            row_id,
                            recipient.id,
                            exc,
                        )
                    # A server failure says nothing of this address, the refusal
                    # that ended a run of them included: a retry keeps its turn.
                    released = "deferred" if recipient.retry else None
                    await _set_outcome(db, row_id, recipient.id, "unknown" if exc.uncertain else released, clock())
                    error = str(exc)
                    break
                await _set_outcome(db, row_id, recipient.id, "delivered", clock())
                sent += 1
                deferred_in_a_row = refused_in_a_row = 0
    except Exception as exc:
        # Anything else stops the batch like an SMTP failure, so it backs off
        # and gives up at MAX_ATTEMPTS; a reservation it left ``sending`` is
        # settled ``unknown`` by the next claim, never sent again.
        logger.exception("Model catalog announcement %s: batch stopped", row_id)
        error = format_exception_message(exc)

    await _finish(
        row_id,
        token,
        sent=sent,
        error=error,
        deferral=deferral,
        deferral_counted=deferral_counted,
        retry_pass=bool(batch) and batch[0].retry,
        at=clock(),
    )
    return sent


# --- Admin announcements, from Settings -----------------------------------

SMTP_NOT_CONFIGURED_DETAIL = "Email is not configured on this platform (SMTP_HOSTNAME), so the users cannot be emailed."
TURNED_OFF_DETAIL = (
    "Model announcement emails are turned off on this platform (MODEL_ANNOUNCEMENT_EMAILS_DISABLED), "
    "so the users cannot be emailed."
)
_REPORTED_STATUSES = ("pending", "sending", "sent", "skipped", "expired", "failed")
# Serializes the admin writes (a save of the default, "Email users"), so two
# concurrent requests can never both queue the same announcement.
_ADMIN_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('model_catalog_admin_announcement', 0))"


class AnnouncementRefusedError(Exception):
    """An admin announcement that cannot be queued; the message is for the admin."""


def emails_turned_off() -> bool:
    """``MODEL_ANNOUNCEMENT_EMAILS_DISABLED``: no model announcement email goes out on this platform."""
    return settings.model_announcement_emails_disabled


def refuse_unless_emails_allowed() -> None:
    """Raise :class:`AnnouncementRefusedError` when this platform may not email a model announcement.

    The one check of every admin path that sends or queues one: the emails
    turned off platform-wide, then no SMTP.
    """
    from app.services.email import is_email_configured

    if emails_turned_off():
        raise AnnouncementRefusedError(TURNED_OFF_DETAIL)
    if not is_email_configured():
        raise AnnouncementRefusedError(SMTP_NOT_CONFIGURED_DETAIL)


def default_model_change(model: str, provider: str | None, previous_default: str | None) -> CatalogChange:
    """What announcing ``model`` as the default tells the users."""
    return CatalogChange(
        new_default=CatalogModel(model, provider or ""),
        previous_default=previous_default if previous_default and previous_default != model else None,
        added=(),
        removed=(),
    )


def render_test_announcement(
    change: CatalogChange, *, recipient_name: str | None, platform_name: str
) -> AnnouncementEmail:
    """The announcement exactly as the users will get it, its subject marked as a test."""
    email = render_announcement(change, recipient_name=recipient_name, platform_name=platform_name)
    return AnnouncementEmail(subject=TEST_SUBJECT_PREFIX + email.subject, html=email.html, text=email.text)


async def announcement_recipient_count(db: AsyncSession) -> int:
    """How many users an admin announcement would be emailed to, as of now."""
    return await _count_eligible(db, SOURCE_ADMIN)


async def previous_default_for(db: AsyncSession, model: str) -> str | None:
    """The default ``model`` replaced, as far as the recorded changes tell."""
    return await db.scalar(
        select(ModelCatalogAnnouncement.previous_default_model)
        .where(
            ModelCatalogAnnouncement.default_model == model,
            ModelCatalogAnnouncement.previous_default_model.is_not(None),
            ModelCatalogAnnouncement.previous_default_model != model,
        )
        .order_by(ModelCatalogAnnouncement.created_at.desc(), ModelCatalogAnnouncement.id.desc())
        .limit(1)
    )


def _refuse_unless_deliverable(model: str | None) -> str:
    if not model:
        raise AnnouncementRefusedError("There is no default model to announce.")
    refuse_unless_emails_allowed()
    return model


def _admin_row(
    *, now: datetime, model: str, provider: str | None, previous_default: str | None, status: str
) -> ModelCatalogAnnouncement:
    return ModelCatalogAnnouncement(
        id=uuid.uuid4(),
        created_at=now,
        source=SOURCE_ADMIN,
        previous_models=[],
        previous_default_model=previous_default,
        models=[],
        default_model=model,
        default_provider=provider or None,
        status=status,
        failed_attempts=0,
    )


async def record_default_model_change(
    db: AsyncSession,
    *,
    previous_default: str | None,
    new_default: str | None,
    new_provider: str | None,
    announce: bool,
    now: datetime | None = None,
) -> ModelCatalogAnnouncement | None:
    """Keep the admin announcements in step with a default model an admin saved.

    Runs in the caller's transaction, committed with the setting. A queued
    admin announcement of another model is dropped (its news is stale); the
    change is recorded, so a later "Email users" can say which model the new
    default replaced; with ``announce`` it is queued for every user. Returns the
    queued row, ``None`` when there is nothing new to tell the users. Raises
    :class:`AnnouncementRefusedError` when ``announce`` cannot be honoured.
    """
    now = now or datetime.now(UTC)
    new_default = new_default or None
    if announce:
        _refuse_unless_deliverable(new_default)
    await db.execute(text(_ADMIN_LOCK_SQL))
    origin = previous_default
    kept: ModelCatalogAnnouncement | None = None
    in_flight = await db.scalars(
        select(ModelCatalogAnnouncement)
        .where(
            ModelCatalogAnnouncement.source == SOURCE_ADMIN,
            # Default announcements only: a catalog change stays true whatever the default.
            ModelCatalogAnnouncement.default_model.is_not(None),
            ModelCatalogAnnouncement.status.in_(_ACTIVE),
        )
        .order_by(ModelCatalogAnnouncement.created_at, ModelCatalogAnnouncement.id)
        .with_for_update()
    )
    for row in in_flight.all():
        if row.default_model == new_default:
            kept = row
            continue
        if row.default_model == origin and not await _has_recipients(db, row.id):
            # Nobody was told about that default: the users' latest news is
            # still the one before it.
            origin = row.previous_default_model
        row.status, row.claim_token, row.next_attempt_at = "superseded", None, None
        await _settle_reserved(db, row.id, now)
    if kept is not None:
        return kept if announce else None
    if new_default is None or new_default == origin:
        return None
    row = _admin_row(
        now=now,
        model=new_default,
        provider=new_provider,
        previous_default=origin,
        status="pending" if announce else "recorded",
    )
    db.add(row)
    return row if announce else None


async def announce_current_default_model(db: AsyncSession, *, now: datetime | None = None) -> ModelCatalogAnnouncement:
    """Queue an announcement of the current default model for every user.

    Also when the default is env-managed: the way to send a change the env seed
    recorded while the emails were off. Runs in the caller's transaction.
    Raises :class:`AnnouncementRefusedError` when there is no default, the emails
    are turned off, no SMTP, or an announcement of that model is already on its way.
    """
    from app.api.v1.platform_settings_helpers import peek_platform_settings

    now = now or datetime.now(UTC)
    await db.execute(text(_ADMIN_LOCK_SQL))
    ps = await peek_platform_settings(db)
    model = _refuse_unless_deliverable(ps.agent_default_model if ps is not None else None)
    in_flight = await db.scalar(
        select(ModelCatalogAnnouncement.id)
        .where(
            ModelCatalogAnnouncement.status.in_(_ACTIVE),
            ModelCatalogAnnouncement.default_model == model,
            or_(
                ModelCatalogAnnouncement.previous_default_model.is_(None),
                ModelCatalogAnnouncement.previous_default_model != model,
            ),
        )
        .limit(1)
    )
    if in_flight is not None:
        raise AnnouncementRefusedError(f"{model_display_name(model)} is already being announced to the users.")
    row = _admin_row(
        now=now,
        model=model,
        provider=ps.agent_default_provider if ps is not None else None,
        previous_default=await previous_default_for(db, model),
        status="pending",
    )
    db.add(row)
    return row


# --- Admin catalog changes, from Settings ---------------------------
# A provider edit in Settings (a model list saved, a provider added or deleted)
# that adds or retires a model the users pick from is recorded as an admin row
# with no default: ``models`` the new models, ``previous_models`` the retired
# ones. Every save is its own row, ``recorded``; what the admin is offered to
# email is their net over ``MODEL_CATALOG_CHANGES_WINDOW_HOURS`` (the draft), and
# the admin chooses what the email says: any model the users can pick as new, any
# model they cannot as retired. The email queued is an admin row of the same
# shape holding that choice.


@dataclass(frozen=True)
class CatalogSnapshot:
    """What the users pick from, and the env-managed catalog, at one moment."""

    offered: ModelCatalog
    env: ModelCatalog


async def read_offered_models(db: AsyncSession) -> ModelCatalog:
    """Every chat model the users can pick now, whichever provider config offers it.

    A provider restricted to some groups and users offers nothing to
    everyone, whom these emails go to.
    """
    from app.llm.router import is_chat_capable_model
    from app.models.llm_config import LLMProviderConfig

    rows = await db.execute(
        select(LLMProviderConfig.provider, LLMProviderConfig.available_models)
        .where(LLMProviderConfig.access_restricted.is_(False))
        .order_by(LLMProviderConfig.created_at, LLMProviderConfig.id)
    )
    offered: dict[str, CatalogModel] = {}
    for provider, available in rows.all():
        for model_id in available or ():
            if isinstance(model_id, str) and model_id and is_chat_capable_model(model_id):
                # One model to the users however many configs offer it.
                offered.setdefault(model_id, CatalogModel(model_id, provider))
    return ModelCatalog(tuple(sorted(offered.values())))


async def snapshot_catalogs(db: AsyncSession) -> CatalogSnapshot:
    return CatalogSnapshot(await read_offered_models(db), await read_model_catalog(db))


def _catalog_change_holds(
    change: CatalogChange, offered: ModelCatalog, *, retired_while_offered: frozenset[str] = frozenset()
) -> bool:
    """Every model ``change`` says is new is still offered, and none it says is retired is back.

    ``retired_while_offered``: retired models the admin announced while the users
    could still pick them. Still offered is what they chose, not news gone stale.
    """
    ids = offered.ids()
    return all(m.id in ids for m in change.added) and not any(
        m.id in ids and m.id not in retired_while_offered for m in change.removed
    )


# The mark, on a retired model of an admin row, that the users could still pick
# it when the admin announced it.
_RETIRED_WHILE_OFFERED = "offered"


def _retired_while_offered(row: ModelCatalogAnnouncement) -> frozenset[str]:
    """The retired models ``row`` announces that the users could still pick when it was queued."""
    return frozenset(
        str(entry["id"])
        for entry in row.previous_models or ()
        if isinstance(entry, dict) and entry.get("id") and entry.get(_RETIRED_WHILE_OFFERED) is True
    )


def _compose_catalog_changes(earlier: CatalogChange, later: CatalogChange) -> CatalogChange:
    """What ``earlier`` then ``later`` changed, for users told of neither.

    A model added then retired again, or retired then offered again, is no news.
    """
    earlier_added, earlier_removed = {m.id for m in earlier.added}, {m.id for m in earlier.removed}
    later_added, later_removed = {m.id for m in later.added}, {m.id for m in later.removed}
    added = {m.id: m for m in earlier.added if m.id not in later_removed}
    added.update({m.id: m for m in later.added if m.id not in earlier_removed})
    removed = {m.id: m for m in earlier.removed if m.id not in later_added}
    removed.update({m.id: m for m in later.removed if m.id not in earlier_added})
    return CatalogChange(
        new_default=None,
        previous_default=None,
        added=tuple(sorted(added.values())),
        removed=tuple(sorted(removed.values())),
    )


async def _stale_admin_news(db: AsyncSession, row: ModelCatalogAnnouncement) -> str | None:
    """Why an admin row's news no longer holds (a later save, a provider deletion, the env seed)."""
    if row.default_model is not None:
        if row.default_model != await _current_default_model(db):
            return f"{row.default_model} is no longer the default model"
        return None
    if not _catalog_change_holds(
        announcement_change(row),
        await read_offered_models(db),
        retired_while_offered=_retired_while_offered(row),
    ):
        return "the models it announces changed again"
    return None


async def _record_env_edit(db: AsyncSession, before: ModelCatalog, after: ModelCatalog, now: datetime) -> None:
    """Make an admin's edit of an env-seeded catalog the one the next boot compares with.

    Under ``LLM_PROVIDER_MODELS_EDITABLE`` the edit outlives the boot, which would
    otherwise read it as an env change and email it: the admin already chose.
    The models only: the default stays the one the env seed last recorded.
    """
    if before.models == after.models:
        return
    latest = await db.scalar(
        select(ModelCatalogAnnouncement)
        .where(ModelCatalogAnnouncement.source == SOURCE_ENV)
        .order_by(ModelCatalogAnnouncement.created_at.desc(), ModelCatalogAnnouncement.id.desc())
        .limit(1)
    )
    known = _known_catalog(latest, after, after) if latest is not None else before
    edited = ModelCatalog(after.models, known.default_model, known.default_provider, known.capabilities)
    if edited == known:
        return
    # The latest env row whatever the clocks of the pods say.
    created_at = max(now, latest.created_at + timedelta(microseconds=1)) if latest is not None else now
    db.add(_new_row(created_at=created_at, before=known, after=edited, status="recorded"))


def changes_window() -> timedelta:
    """How far back the provider saves an admin is offered to email are consolidated."""
    return timedelta(hours=settings.model_catalog_changes_window_hours)


@dataclass(frozen=True)
class CatalogDraft:
    """The model changes saved and not emailed yet: the net of the saves of the window.

    ``version`` is the latest save it covers, the id the admin reviews it by and
    emailing it consumes up to (a save made meanwhile stays in the next draft).
    """

    version: ModelCatalogAnnouncement
    change: CatalogChange
    first_saved_at: datetime
    saves: int


def _saved_catalog_edits():
    return select(ModelCatalogAnnouncement).where(
        ModelCatalogAnnouncement.source == SOURCE_ADMIN,
        ModelCatalogAnnouncement.default_model.is_(None),
        ModelCatalogAnnouncement.status == "recorded",
    )


def _still_true(
    change: CatalogChange, offered: ModelCatalog, *, retired_while_offered: frozenset[str] = frozenset()
) -> CatalogChange:
    """``change`` without what the catalog no longer says: a new model gone, a retired one back.

    A new model is announced under the provider that offers it now. A model an
    admin announced as retired while the users could still pick it stays.
    """
    live = {m.id: m for m in offered.models}
    return CatalogChange(
        new_default=None,
        previous_default=None,
        added=tuple(sorted({live[m.id] for m in change.added if m.id in live})),
        removed=tuple(m for m in change.removed if m.id not in live or m.id in retired_while_offered),
    )


async def catalog_draft(
    db: AsyncSession, *, now: datetime | None = None, offered: ModelCatalog | None = None
) -> CatalogDraft | None:
    """The model changes the admin is offered to email, ``None`` when there is nothing to tell.

    Every save of ``MODEL_CATALOG_CHANGES_WINDOW_HOURS`` composed in order (a model
    retired then offered again, or added then retired, cancels out; the latest save
    of a model wins, whatever provider it belongs to), held against the models
    offered now. ``offered``: those models, read when omitted.
    """
    now = now or datetime.now(UTC)
    rows = (
        await db.scalars(
            _saved_catalog_edits()
            .where(ModelCatalogAnnouncement.created_at > now - changes_window())
            .order_by(ModelCatalogAnnouncement.created_at, ModelCatalogAnnouncement.id)
        )
    ).all()
    if not rows:
        return None
    net = CatalogChange(new_default=None, previous_default=None, added=(), removed=())
    for row in rows:
        net = _compose_catalog_changes(net, announcement_change(row))
    net = _still_true(
        net,
        offered if offered is not None else await read_offered_models(db),
        # An announcement that went back to the draft keeps what its admin chose.
        retired_while_offered=frozenset().union(*(_retired_while_offered(row) for row in rows)),
    )
    if not net.announces:
        return None
    return CatalogDraft(version=rows[-1], change=net, first_saved_at=rows[0].created_at, saves=len(rows))


async def _next_admin_catalog_time(db: AsyncSession, now: datetime) -> datetime:
    """``now``, or just after the latest admin catalog row: saves compose in the order they were made."""
    latest = await db.scalar(
        select(func.max(ModelCatalogAnnouncement.created_at)).where(
            ModelCatalogAnnouncement.source == SOURCE_ADMIN,
            ModelCatalogAnnouncement.default_model.is_(None),
        )
    )
    return max(now, latest + timedelta(microseconds=1)) if latest is not None else now


def _admin_catalog_change_row(
    *,
    created_at: datetime,
    change: CatalogChange,
    status: str,
    retired_while_offered: frozenset[str] = frozenset(),
) -> ModelCatalogAnnouncement:
    return ModelCatalogAnnouncement(
        id=uuid.uuid4(),
        created_at=created_at,
        source=SOURCE_ADMIN,
        previous_models=[
            {**entry, _RETIRED_WHILE_OFFERED: True} if entry["id"] in retired_while_offered else entry
            for entry in ModelCatalog(change.removed).models_json()
        ],
        previous_default_model=None,
        models=ModelCatalog(change.added).models_json(),
        default_model=None,
        default_provider=None,
        status=status,
        failed_attempts=0,
    )


async def record_provider_edit(
    db: AsyncSession, before: CatalogSnapshot, *, now: datetime | None = None
) -> CatalogDraft | None:
    """Record what an admin's provider edit changed in the models the users pick from.

    Runs in the caller's transaction once the edit is flushed, committed with it.
    Nothing is sent: the save is kept, ``recorded``, as its own change, and the
    admin is offered the draft (:func:`catalog_draft`). An email queued and sent
    to nobody yet goes back to the draft: the users were told of neither. Returns
    the draft after this save, ``None`` when the save changed nothing the users
    pick from or leaves nothing to tell them.
    """
    now = now or datetime.now(UTC)
    after = await snapshot_catalogs(db)
    await _record_env_edit(db, before.env, after.env, now)
    change = diff_catalogs(before.offered, after.offered)
    if not change.announces:
        return None
    await db.execute(text(_ADMIN_LOCK_SQL))
    queued = await db.scalars(
        select(ModelCatalogAnnouncement)
        .where(
            ModelCatalogAnnouncement.source == SOURCE_ADMIN,
            ModelCatalogAnnouncement.default_model.is_(None),
            ModelCatalogAnnouncement.status.in_(_ACTIVE),
        )
        .with_for_update()
    )
    for row in queued.all():
        if await _has_recipients(db, row.id):
            # Already reaching the users: its claim drops it once it no longer holds.
            continue
        row.status, row.claim_token, row.next_attempt_at = "recorded", None, None
    created_at = await _next_admin_catalog_time(db, now)
    db.add(_admin_catalog_change_row(created_at=created_at, change=change, status="recorded"))
    await db.flush()
    return await catalog_draft(db, now=created_at, offered=after.offered)


# The admin chooses what the email says, within these bounds.
MAX_ANNOUNCED_MODELS = 100
MODEL_ID_MAX_CHARS = 200
# What a model id looks like (``anthropic.claude-...-v1:0``, ``org/model``,
# ``llama3:8b-q4_K_M``, ``claude-...@20240620``): the email names a retired
# model as typed, so it can never carry a sentence (#5045). Mirrored by
# ``MODEL_ID_PATTERN`` in ``frontend/src/lib/model-announcements.ts``.
MODEL_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*"
_MODEL_ID_RE = re.compile(MODEL_ID_PATTERN)
_KNOWN_MODELS_SCANNED_ROWS = 500


def is_model_id(value: object) -> bool:
    """``value`` is shaped like a model id, within ``MODEL_ID_MAX_CHARS``."""
    return isinstance(value, str) and len(value) <= MODEL_ID_MAX_CHARS and _MODEL_ID_RE.fullmatch(value) is not None


class AnnouncementSelectionError(ValueError):
    """Models the admin chose that cannot be announced as they are."""


async def formerly_offered_models(db: AsyncSession, offered: ModelCatalog) -> tuple[CatalogModel, ...]:
    """The models the platform recorded once and the users can no longer pick.

    Read from the latest announcements of any source, each under the provider it
    was last recorded with: the suggestions for a retired model.
    """
    rows = await db.execute(
        select(
            ModelCatalogAnnouncement.models,
            ModelCatalogAnnouncement.previous_models,
            ModelCatalogAnnouncement.default_model,
            ModelCatalogAnnouncement.default_provider,
            ModelCatalogAnnouncement.previous_default_model,
        )
        .order_by(ModelCatalogAnnouncement.created_at.desc(), ModelCatalogAnnouncement.id.desc())
        .limit(_KNOWN_MODELS_SCANNED_ROWS)
    )
    live = offered.ids()
    known: dict[str, CatalogModel] = {}
    for models, previous, default_model, default_provider, previous_default in rows.all():
        recorded = [
            *ModelCatalog.from_json(models, None, None).models,
            *ModelCatalog.from_json(previous, None, None).models,
        ]
        if default_model:
            recorded.append(CatalogModel(default_model, default_provider or ""))
        if previous_default:
            recorded.append(CatalogModel(previous_default, ""))
        for model in recorded:
            if model.id not in live:
                current = known.get(model.id)
                if current is None or (not current.provider and model.provider):
                    known[model.id] = model
    return tuple(sorted(known.values()))


# The providers a retired model may be announced under: the ones the email names.
ANNOUNCEABLE_PROVIDERS = frozenset(_PROVIDER_NAMES)


def _chosen_models(entries: Sequence[str | CatalogModel], what: str, *, free_text: bool) -> dict[str, str]:
    """The ids chosen as ``what`` models, in order, each with the provider named for it ("" for none).

    ``free_text``: any id at all may be chosen (the retired models), so it must
    look like a model id (:func:`is_model_id`); otherwise the ids are checked
    against the models offered afterwards and only bounded here.
    """
    chosen: dict[str, str] = {}
    for entry in entries:
        raw_id, raw_provider = (entry.id, entry.provider) if isinstance(entry, CatalogModel) else (entry, "")
        model_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not model_id:
            continue
        if free_text and not is_model_id(model_id):
            raise AnnouncementSelectionError(
                f"A {what} model must be a model id: at most {MODEL_ID_MAX_CHARS} letters, digits and . _ : / @ + - "
                "characters, starting with a letter or a digit."
            )
        if len(model_id) > MODEL_ID_MAX_CHARS or not model_id.isprintable():
            raise AnnouncementSelectionError(
                f"A {what} model must be named in at most {MODEL_ID_MAX_CHARS} printable characters."
            )
        provider = raw_provider.strip().lower() if isinstance(raw_provider, str) else ""
        if provider and provider not in ANNOUNCEABLE_PROVIDERS:
            raise AnnouncementSelectionError(f"Unknown provider for {model_id}: {provider[:64]}.")
        if not chosen.get(model_id):
            chosen[model_id] = provider
    if len(chosen) > MAX_ANNOUNCED_MODELS:
        raise AnnouncementSelectionError(f"At most {MAX_ANNOUNCED_MODELS} {what} models can be announced at once.")
    return chosen


async def resolve_model_selection(
    db: AsyncSession,
    added: Sequence[str],
    removed: Sequence[str | CatalogModel],
    *,
    offered: ModelCatalog | None = None,
) -> CatalogChange:
    """The announcement of ``added`` as new models and ``removed`` as retired ones.

    New: any model the users can pick now, whether a save added it or not, under
    the provider that offers it. Retired: any model at all, the ones the
    users can still pick included - the admin may announce a retirement before
    it happens - under the provider named for it, else the one offering it, else
    the one it was last recorded with (:func:`formerly_offered_models`), else
    none; a provider the email cannot name (the embedding server) is never
    guessed. Raises :class:`AnnouncementSelectionError` for a model that is not
    offered as new, a model both new and retired, an unknown provider, or
    nothing chosen.
    """
    added_ids = list(_chosen_models(added, "new", free_text=False))
    retired = _chosen_models(removed, "retired", free_text=True)
    if not added_ids and not retired:
        raise AnnouncementSelectionError("Choose at least one new or retired model to announce.")
    both = [model_id for model_id in added_ids if model_id in retired]
    if both:
        raise AnnouncementSelectionError(f"A model cannot be announced both as new and as retired: {', '.join(both)}.")
    if offered is None:
        offered = await read_offered_models(db)
    live = {m.id: m for m in offered.models}
    missing = [model_id for model_id in added_ids if model_id not in live]
    if missing:
        raise AnnouncementSelectionError(
            f"Only a model the users can pick can be announced as new: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not offered."
        )
    unnamed = [model_id for model_id, provider in retired.items() if not provider and model_id not in live]
    known = {m.id: m.provider for m in await formerly_offered_models(db, offered)} if unnamed else {}

    def guessed_provider(model_id: str) -> str:
        provider = live[model_id].provider if model_id in live else known.get(model_id, "")
        return provider if provider in ANNOUNCEABLE_PROVIDERS else ""

    return CatalogChange(
        new_default=None,
        previous_default=None,
        added=tuple(sorted(live[model_id] for model_id in added_ids)),
        removed=tuple(
            sorted(
                CatalogModel(model_id, provider or guessed_provider(model_id)) for model_id, provider in retired.items()
            )
        ),
    )


PROVIDER_CATALOG_TIMEOUT_SECONDS = 20.0
PROVIDER_CATALOG_MAX_MODELS = 2000


@dataclass(frozen=True)
class ProviderCatalog:
    """The chat models one provider config lists, or why they could not be listed."""

    config_id: uuid.UUID
    provider: str
    label: str | None
    models: tuple[str, ...] = ()
    error: str | None = None
    # The provider listed more than ``PROVIDER_CATALOG_MAX_MODELS``: ``models`` holds the first ones.
    truncated: bool = False


async def list_provider_catalogs(db: AsyncSession) -> tuple[ProviderCatalog, ...]:
    """Ask every configured provider which chat models it serves, all at once and bounded.

    The models the admin can announce as retired go beyond what the platform
    recorded: a model a provider still serves that the platform stopped
    offering, or never offered. A provider that fails or does not answer within
    ``PROVIDER_CATALOG_TIMEOUT_SECONDS`` costs its own list (``error``), never
    the others'. An outbound call per provider: gate it like any model listing.
    Only the providers an email can name are asked (an embedding server is not).
    Commits ``db`` once the configs are read (nothing is written) so the
    fan-out does not hold a pooled connection.
    """
    from app.core.database import release_db_connection_before_loop
    from app.llm.router import PROVIDER_MAP, is_chat_capable_model, provider_instance
    from app.models.llm_config import LLMProviderConfig
    from app.services.vault import SecretOwner, reveal_ciphertext

    configs = (
        await db.execute(
            select(
                LLMProviderConfig.id,
                LLMProviderConfig.provider,
                LLMProviderConfig.display_name,
                LLMProviderConfig.api_key_enc,
            )
            .where(LLMProviderConfig.provider.in_(sorted(ANNOUNCEABLE_PROVIDERS)))
            .order_by(LLMProviderConfig.created_at, LLMProviderConfig.id)
        )
    ).all()
    await release_db_connection_before_loop(db)

    async def _list(config_id: uuid.UUID, provider: str, label: str | None, api_key_enc: str) -> ProviderCatalog:
        label = (label or "").strip() or None
        provider_cls = PROVIDER_MAP.get(provider)
        if provider_cls is None:
            return ProviderCatalog(config_id, provider, label, error=f"Unsupported provider: {provider}")
        try:
            api_key = await reveal_ciphertext(
                api_key_enc, SecretOwner(kind="llm_provider", id=str(config_id), field="api_key")
            )
            client = provider_instance(provider_cls, api_key or "", config_id)
            listed = await asyncio.wait_for(client.list_models(), timeout=PROVIDER_CATALOG_TIMEOUT_SECONDS)
        except Exception as exc:
            # One provider's failure is reported with its list, never raised.
            reason = (
                f"no answer within {PROVIDER_CATALOG_TIMEOUT_SECONDS:g} seconds"
                if isinstance(exc, TimeoutError)
                else format_exception_message(exc)
            )
            logger.warning(
                "Model announcements: could not list the models of provider config %s (%s): %s",
                config_id,
                provider,
                reason,
            )
            return ProviderCatalog(config_id, provider, label, error=_reported_error(reason))
        chat_models = sorted({m for m in listed or () if is_model_id(m) and is_chat_capable_model(m)})
        return ProviderCatalog(
            config_id,
            provider,
            label,
            models=tuple(chat_models[:PROVIDER_CATALOG_MAX_MODELS]),
            truncated=len(chat_models) > PROVIDER_CATALOG_MAX_MODELS,
        )

    return tuple(await asyncio.gather(*(_list(*config) for config in configs)))


def unoffered_catalog_models(catalogs: Sequence[ProviderCatalog], offered: ModelCatalog) -> tuple[CatalogModel, ...]:
    """The models the providers list that the users cannot pick, each under the first config listing it."""
    live = offered.ids()
    found: dict[str, CatalogModel] = {}
    for catalog in catalogs:
        for model_id in catalog.models:
            if model_id not in live:
                found.setdefault(model_id, CatalogModel(model_id, catalog.provider))
    return tuple(sorted(found.values()))


def _admin_catalog_row(announcement_id: uuid.UUID):
    return select(ModelCatalogAnnouncement).where(
        ModelCatalogAnnouncement.id == announcement_id,
        ModelCatalogAnnouncement.source == SOURCE_ADMIN,
        ModelCatalogAnnouncement.default_model.is_(None),
    )


async def announce_model_changes(
    db: AsyncSession,
    *,
    added: Sequence[str],
    removed: Sequence[str | CatalogModel],
    draft_id: uuid.UUID | None,
    now: datetime | None = None,
) -> ModelCatalogAnnouncement:
    """Queue, for every user, the email of the models the admin chose as new and as retired.

    Runs in the caller's transaction. ``draft_id``: the ``version`` of the draft
    the admin reviewed (``None`` for an announcement of their own). Its saves are
    consumed, the models the admin left out included; a save made since stays in
    the draft. Raises :class:`LookupError` for an unknown draft,
    :class:`AnnouncementRefusedError` when nothing can be queued (the emails
    turned off, no SMTP, the draft already emailed) and :class:`AnnouncementSelectionError` for a
    selection that cannot be announced (:func:`resolve_model_selection`).
    """
    now = now or datetime.now(UTC)
    refuse_unless_emails_allowed()
    await db.execute(text(_ADMIN_LOCK_SQL))
    reviewed_until: datetime | None = None
    if draft_id is not None:
        version = await db.scalar(_admin_catalog_row(draft_id))
        if version is None:
            raise LookupError(str(draft_id))
        if version.status != "recorded":
            raise AnnouncementRefusedError(
                "These model changes were already emailed or queued: review the latest changes and try again."
            )
        reviewed_until = version.created_at
    offered = await read_offered_models(db)
    change = await resolve_model_selection(db, added, removed, offered=offered)
    if reviewed_until is not None:
        await db.execute(
            update(ModelCatalogAnnouncement)
            .where(
                ModelCatalogAnnouncement.source == SOURCE_ADMIN,
                ModelCatalogAnnouncement.default_model.is_(None),
                ModelCatalogAnnouncement.status == "recorded",
                ModelCatalogAnnouncement.created_at <= reviewed_until,
            )
            .values(status="superseded", claim_token=None, next_attempt_at=None)
            .execution_options(synchronize_session="fetch")
        )
    live = offered.ids()
    row = _admin_catalog_change_row(
        created_at=await _next_admin_catalog_time(db, now),
        change=change,
        status="pending",
        retired_while_offered=frozenset(m.id for m in change.removed if m.id in live),
    )
    db.add(row)
    return row


def announcement_change(row: ModelCatalogAnnouncement) -> CatalogChange:
    """What ``row``'s email tells the users."""
    return diff_catalogs(_catalog_before(row), _catalog_after(row))


_LAST_ERROR_REPORTED_CHARS = 300


@dataclass(frozen=True)
class AnnouncementProgress:
    row: ModelCatalogAnnouncement
    change: CatalogChange
    delivered: int = 0
    refused: int = 0
    unknown: int = 0
    # Open or failed: the users it has not reached yet, those being emailed right now included.
    remaining: int = 0
    # Open: the open announcements recorded before it, which go out first.
    queued_behind: int = 0
    # Open after a failed batch: when the next batch may run. Open or failed:
    # the failed batches in a row and the last error.
    retry_at: datetime | None = None
    failed_attempts: int = 0
    last_error: str | None = None
    # Open env change waiting for the env catalog to settle: when it may go out.
    held_until: datetime | None = None
    # Open behind an older one: that one's next attempt, when it is waiting.
    blocked_until: datetime | None = None
    # Open: the addresses the server refused for now, retried once after
    # everyone else (counted in ``remaining``); when that retry runs, once
    # nobody else is left, or whether it is under way (nobody else left and
    # nothing to wait for); and the server's last reply to one of them.
    deferred: int = 0
    deferred_retry_at: datetime | None = None
    deferred_retrying: bool = False
    last_refusal: str | None = None


def _reported_error(error: str | None) -> str | None:
    folded = " ".join((error or "").split())
    if len(folded) > _LAST_ERROR_REPORTED_CHARS:
        folded = folded[: _LAST_ERROR_REPORTED_CHARS - 3].rstrip() + "..."
    return folded or None


async def latest_announcement(db: AsyncSession) -> AnnouncementProgress | None:
    """The latest announcement that was, is being or could not be sent, with its progress."""
    from app.models.user import User

    row = await db.scalar(
        select(ModelCatalogAnnouncement)
        .where(ModelCatalogAnnouncement.status.in_(_REPORTED_STATUSES))
        .order_by(ModelCatalogAnnouncement.created_at.desc(), ModelCatalogAnnouncement.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    outcomes = dict(
        (
            await db.execute(
                select(ModelCatalogAnnouncementRecipient.status, func.count())
                .where(ModelCatalogAnnouncementRecipient.announcement_id == row.id)
                .group_by(ModelCatalogAnnouncementRecipient.status)
            )
        ).all()
    )
    active = row.status in _ACTIVE
    # A reservation on an open row is a send in progress; on a closed one, a
    # send nobody settled.
    in_progress = outcomes.get("sending", 0) if active else 0
    remaining = queued_behind = not_served = deferred = 0
    blocked_until: datetime | None = None
    # An address refused for now is still to reach while the row may retry it,
    # refused once it no longer can.
    late_refusals = 0 if active else outcomes.get("deferred", 0)
    if active or row.status == "failed":
        eligible = _eligible_users(row.source)
        not_served = int(await db.scalar(select(func.count(User.id)).where(*eligible, _not_served(row.id))) or 0)
        if active:
            deferred = int(await db.scalar(select(func.count(User.id)).where(*eligible, _deferred(row.id))) or 0)
        remaining = in_progress + not_served + deferred
    if active:
        ahead = (
            ModelCatalogAnnouncement.status.in_(_ACTIVE),
            or_(
                ModelCatalogAnnouncement.created_at < row.created_at,
                and_(
                    ModelCatalogAnnouncement.created_at == row.created_at,
                    ModelCatalogAnnouncement.id < row.id,
                ),
            ),
        )
        queued_behind = int(await db.scalar(select(func.count(ModelCatalogAnnouncement.id)).where(*ahead)) or 0)
        if queued_behind:
            # Only the oldest open one is ever sent: its next attempt is when the queue moves.
            head = (
                await db.execute(
                    select(ModelCatalogAnnouncement.status, ModelCatalogAnnouncement.next_attempt_at)
                    .where(*ahead)
                    .order_by(ModelCatalogAnnouncement.created_at, ModelCatalogAnnouncement.id)
                    .limit(1)
                )
            ).one_or_none()
            if head is not None and head.status == "pending":
                blocked_until = head.next_attempt_at
    # Failed batches, not refused addresses: a row every address refused has
    # none, and its outcome counts say why.
    failing = (active or row.status == "failed") and (row.failed_attempts or 0) > 0
    waiting = row.status == "pending" and not failing and row.next_attempt_at is not None
    # Everyone else emailed: the addresses refused for now are all that is left,
    # waiting for their one retry, else being retried (the next batch takes them).
    only_retries = active and not failing and deferred > 0 and not_served == 0
    retry_wait = only_retries and waiting
    retrying = only_retries and not waiting
    # Any other wait with no failed batch is the settle hold.
    held = waiting and not retry_wait
    return AnnouncementProgress(
        row=row,
        change=announcement_change(row),
        delivered=outcomes.get("delivered", 0),
        refused=outcomes.get("refused", 0) + late_refusals,
        unknown=outcomes.get("unknown", 0) + outcomes.get("sending", 0) - in_progress,
        remaining=remaining,
        queued_behind=queued_behind,
        retry_at=row.next_attempt_at if failing and row.status == "pending" else None,
        failed_attempts=(row.failed_attempts or 0) if failing else 0,
        last_error=_reported_error(row.last_error) if failing else None,
        held_until=row.next_attempt_at if held else None,
        blocked_until=blocked_until,
        deferred=deferred,
        deferred_retry_at=row.next_attempt_at if retry_wait else None,
        deferred_retrying=retrying,
        last_refusal=_reported_error(row.last_error) if deferred and not failing else None,
    )
