# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The published link of a custom dashboard: anyone with the URL sees it, without signing in.

Like a deck's public link (``presentation_public_links``), a capability URL its
managers hand to people with no account here, and the one way into a
dashboard that skips its grants. What differs is the data: a dashboard reads
live sources (a platform's figures, an assignment's answer, a variable), and
a visitor has no access of their own to read them with. So a published link
never reads live with anyone's credentials on a visitor's behalf; it shows a
**snapshot**:

* **What the snapshot holds** (:func:`read_snapshot`): each data source's
  result - and so each visualization's, which draws its primary source - read
  once on the server with the access of the person who published the link,
  the definition's default tokens put in. A question to an agent is never
  asked for it, nor its last answer read, directly or through a derived
  source (:func:`asks_an_agent`). Links into a platform (``open_url``, every ``_url`` and ``link`` of the data) are
  left out: a visitor cannot follow them, and they name internal addresses.
* **How fresh** (:func:`current_snapshot`): read again on a visit once it is
  older than the link's ``refresh_minutes`` (15 minutes at least) or the
  dashboard changed since, by one request at a time per link (a Redis lock,
  fail-closed: without Redis the snapshot is served as it is). A manager may
  read it again at once (``POST .../public-link/refresh``).
* **What a visitor receives** (:func:`public_payload`): the definition
  reduced to what draws - every source replaced by a plain one with its name
  and shape (no platform query, integration, assignment, agent, question or
  variable), no inputs, no click actions, no links - with the snapshot and
  when it was read. Its files are those the definition draws.
* **Who it stands for**: the link answers only while its publisher may still
  read the dashboard (:func:`resolve_public_link`): a share revoked, a
  publisher deleted or turned inactive, and the link answers like one nobody
  issued.
* **The admin switch** ``dashboard_public_links_enabled`` (Settings >
  Security, off by default) is read on every request: off, every link answers
  404 and none can be published. The rows are kept.

The token is the deck links' (``presentation_public_links.mint_token``):
looked up by its SHA-256, kept Fernet-encrypted for the managers (invariant 2).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_value, encrypt_value, hash_token
from app.core.security_options import security_option
from app.models.custom_dashboard import CustomDashboard, CustomDashboardPublicLink
from app.models.user import User
from app.services.dashboard_tokens import starting_tokens
from app.services.presentation_public_links import mint_token, well_formed_token

logger = logging.getLogger(__name__)

#: The web app page a published link opens (``DashboardPublic.tsx``).
PUBLIC_LINK_ROUTE = "/public/dashboards"

#: How often a snapshot may be read again, in minutes: the choices a manager has.
REFRESH_CHOICES: tuple[int, ...] = (15, 60, 240, 1440)
DEFAULT_REFRESH_MINUTES = 60

#: A snapshot is at most this large (JSON bytes); the sources past it are stored as having no result.
MAX_SNAPSHOT_BYTES = 2_000_000

#: How long one refresh may hold its link's lock.
REFRESH_LOCK_SECONDS = 120

#: The answer while the admin switch is off.
PUBLIC_LINKS_OFF_DETAIL = (
    "Published dashboard links are turned off on this platform by an administrator "
    "(Settings > Access and security > Published dashboard links)."
)

OUTCOME_CREATED = "enabled"
OUTCOME_REGENERATED = "regenerated"
OUTCOME_KEPT = "kept"
OUTCOME_DISABLED = "disabled"

#: A source whose result was not kept (an agent's answer, a snapshot over its size).
_NOT_KEPT: dict[str, Any] = {"status": "error", "code": "no_result", "open_url": None}


def public_links_enabled() -> bool:
    """Whether published links answer and may be created: the admin switch, with the Dashboard Studio on."""
    from app.core.feature_flags import is_feature_enabled

    return bool(security_option("dashboard_public_links_enabled")) and is_feature_enabled("DASHBOARD_STUDIO")


def public_link_path(token: str) -> str:
    return f"{PUBLIC_LINK_ROUTE}/{token}"


def public_link_url(token: str) -> str:
    """The absolute address a person pastes into a message."""
    from app.services.email import platform_url

    return f"{platform_url()}{public_link_path(token)}"


def link_token(link: CustomDashboardPublicLink) -> str | None:
    """The token of a stored link, or ``None`` when ``SECRET_KEY`` no longer decrypts it (regenerate it)."""
    try:
        return decrypt_value(link.token_enc)
    except (InvalidToken, ValueError):
        logger.warning("The published link of dashboard %s can no longer be decrypted", link.dashboard_id)
        return None


# ── Reads ─────────────────────────────────────────────────────────────────────


async def load_public_link(db: AsyncSession, dashboard_id: uuid.UUID) -> CustomDashboardPublicLink | None:
    result = await db.execute(
        select(CustomDashboardPublicLink).where(CustomDashboardPublicLink.dashboard_id == dashboard_id)
    )
    return result.scalar_one_or_none()


async def resolve_publisher(db: AsyncSession, dashboard: CustomDashboard, link: CustomDashboardPublicLink) -> User | None:
    """The link's publisher while they may still edit the dashboard (manage its link), else ``None``."""
    from app.services.custom_dashboards import load_dashboard_for

    if link.created_by_user_id is None:
        return None
    user = await db.get(User, link.created_by_user_id)
    if user is None or not user.is_active:
        return None
    editable = await load_dashboard_for(db, dashboard.id, user_id=user.id, is_admin=user.is_admin, need="edit")
    return user if editable is not None else None


async def follow_author(db: AsyncSession, dashboard_id: uuid.UUID, author_id: uuid.UUID | None) -> None:
    """After *author_id* changed what a published dashboard reads (its definition, or its link's ceiling), the
    link reads with THEIR access: they become its publisher and the snapshot kept is dropped.

    Without it, someone who may edit the dashboard could make it read, with the
    publisher's access, data they cannot see themselves, and read it back
    through the link. A change by no one known (``None``) leaves the link with
    no publisher: it shows nothing until someone publishes it again. Nothing
    without a link, nothing when the author already publishes it. Commits nothing.
    """
    await db.execute(
        update(CustomDashboardPublicLink)
        .where(
            CustomDashboardPublicLink.dashboard_id == dashboard_id,
            CustomDashboardPublicLink.created_by_user_id.is_distinct_from(author_id),
        )
        .values(created_by_user_id=author_id, snapshot=None, snapshot_at=None, snapshot_revision=None)
    )


async def resolve_public_link(
    db: AsyncSession, token: object
) -> tuple[CustomDashboard, CustomDashboardPublicLink, User] | None:
    """The dashboard *token* opens, its link and its publisher - or ``None``, every refusal alike.

    ``None`` for the switch off, a malformed, unknown, regenerated or turned
    off token, and a publisher who may no longer read the dashboard: the
    answer never tells a dead link from one that never existed.
    """
    if not public_links_enabled() or not well_formed_token(token):
        return None
    row = (
        await db.execute(
            select(CustomDashboard, CustomDashboardPublicLink)
            .join(CustomDashboardPublicLink, CustomDashboardPublicLink.dashboard_id == CustomDashboard.id)
            .where(CustomDashboardPublicLink.token_hash == hash_token(str(token)))
        )
    ).first()
    if row is None:
        return None
    dashboard, link = row[0], row[1]
    publisher = await resolve_publisher(db, dashboard, link)
    return (dashboard, link, publisher) if publisher is not None else None


# ── Writes ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicLinkChange:
    """What :func:`enable_public_link` did."""

    outcome: str
    token: str | None

    @property
    def changed(self) -> bool:
        return self.outcome != OUTCOME_KEPT


async def enable_public_link(
    db: AsyncSession,
    dashboard_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    regenerate: bool = False,
    refresh_minutes: int | None = None,
    marking_ceiling: list[str] | None = None,
) -> PublicLinkChange:
    """Publish the dashboard as a link, or hand back the one it has; ``regenerate`` issues a new token.

    The person publishing (or regenerating) becomes the publisher: the
    snapshot is read with THEIR access from then on, so the one kept is
    dropped. Upserts on the unique ``dashboard_id``; the caller checked the
    switch and that *actor_id* may manage the dashboard. Commits nothing.
    """
    token = mint_token()
    fields: dict[str, Any] = {
        "token_hash": hash_token(token),
        "token_enc": encrypt_value(token),
        "created_by_user_id": actor_id,
        "created_at": datetime.now(UTC),
        "snapshot": None,
        "snapshot_at": None,
        "snapshot_revision": None,
    }
    if refresh_minutes is not None:
        fields["refresh_minutes"] = refresh_minutes
    if marking_ceiling is not None:
        fields["marking_ceiling"] = list(marking_ceiling)
    if regenerate:
        rotated = await db.execute(
            update(CustomDashboardPublicLink)
            .where(CustomDashboardPublicLink.dashboard_id == dashboard_id)
            .values(**fields)
            .returning(CustomDashboardPublicLink.id)
        )
        if rotated.scalar_one_or_none() is not None:
            return PublicLinkChange(OUTCOME_REGENERATED, token)
    inserted = await db.execute(
        pg_insert(CustomDashboardPublicLink)
        .values(id=uuid.uuid4(), dashboard_id=dashboard_id, **fields)
        .on_conflict_do_nothing(index_elements=[CustomDashboardPublicLink.dashboard_id])
        .returning(CustomDashboardPublicLink.id)
    )
    if inserted.scalar_one_or_none() is not None:
        return PublicLinkChange(OUTCOME_CREATED, token)
    existing = await load_public_link(db, dashboard_id)
    return PublicLinkChange(OUTCOME_KEPT, link_token(existing) if existing is not None else None)


async def set_link_options(
    db: AsyncSession, dashboard_id: uuid.UUID, *, refresh_minutes: int | None = None, marking_ceiling: list[str] | None = None
) -> bool | None:
    """How often the snapshot is read again, and its marking ceiling (a new one drops the snapshot kept: the next
    visit reads it under the ceiling); ``None`` without a link. Commits nothing."""
    values: dict[str, Any] = {}
    if refresh_minutes is not None:
        values["refresh_minutes"] = refresh_minutes
    if marking_ceiling is not None:
        values.update(marking_ceiling=list(marking_ceiling), snapshot=None, snapshot_at=None, snapshot_revision=None)
    if not values:
        return True if await load_public_link(db, dashboard_id) is not None else None
    changed = await db.execute(
        update(CustomDashboardPublicLink)
        .where(CustomDashboardPublicLink.dashboard_id == dashboard_id)
        .values(**values)
        .returning(CustomDashboardPublicLink.id)
    )
    return True if changed.scalar_one_or_none() is not None else None


async def disable_public_link(db: AsyncSession, dashboard_id: uuid.UUID) -> bool:
    """Delete the dashboard's published link: its URL stops answering at once. ``False`` when it had none."""
    removed = await db.execute(
        delete(CustomDashboardPublicLink)
        .where(CustomDashboardPublicLink.dashboard_id == dashboard_id)
        .returning(CustomDashboardPublicLink.id)
    )
    return removed.scalar_one_or_none() is not None


# ── The snapshot ──────────────────────────────────────────────────────────────




#: The keys a closed shape keeps a link under: a row's ``_url``, an item's, an event's, an outcome's or an action's ``link``.
_LINK_KEYS = frozenset({"_url", "link"})


def _strip_links(value: object) -> object:
    if isinstance(value, dict):
        return {k: _strip_links(v) for k, v in value.items() if k not in _LINK_KEYS}
    if isinstance(value, list):
        return [_strip_links(v) for v in value]
    return value


def _without_links(data: dict) -> dict:
    """A result as a visitor may receive it: no link into a platform, at the top or anywhere in its data."""
    clean = {**data, "open_url": None}
    if isinstance(clean.get("data"), dict):
        clean["data"] = _strip_links(clean["data"])
    return clean


def _reads_one_of(definition: dict, source_id: str, kinds: tuple[str, ...]) -> bool:
    """Whether *source_id* is a source of one of *kinds*, or derives from one."""
    from app.services.dashboard_definition import MAX_CHAIN_DEPTH

    sources = definition.get("dataSources") or {}
    sid: object = source_id
    for _ in range(MAX_CHAIN_DEPTH + 1):
        source = sources.get(sid) if isinstance(sid, str) else None
        if not isinstance(source, dict):
            return False
        if source.get("type") in kinds:
            return True
        if source.get("type") != "derived":
            return False
        sid = (source.get("options") or {}).get("extends")
    return True


def asks_an_agent(definition: dict, source_id: str) -> bool:
    """Whether *source_id* is a question to an agent, or derives from one (its answer is a person's own)."""
    return _reads_one_of(definition, source_id, ("ask",))


def held_to_ceiling(definition: dict, source_id: str) -> bool:
    """Whether *source_id* can be held to a marking ceiling: not an agent's written answer, nor derived from one
    (text an agent wrote can quote anything)."""
    return not _reads_one_of(definition, source_id, ("ask", "assignment"))


async def read_snapshot(
    db: AsyncSession, definition: dict, dashboard_id: uuid.UUID, user_id: uuid.UUID, marking_ceiling: tuple[str, ...] = ()
) -> dict:
    """Every source's result as *user_id* reads it now, and each visualization's (its primary source's); under a
    *marking_ceiling*, OpenCTI's data marked above it left out, and an agent's answer not kept at all."""
    from app.services.dashboard_sources import read_source
    try:
        tokens = starting_tokens(definition)
    except ValueError:
        tokens = {}
    sources: dict[str, dict] = {}
    size = 0
    for sid in definition.get("dataSources") or {}:
        if asks_an_agent(definition, sid) or (marking_ceiling and not held_to_ceiling(definition, sid)):
            # A question is asked by a person in the app, never for a snapshot - nor read through a chain.
            sources[sid] = dict(_NOT_KEPT)
            continue
        try:
            read = await read_source(db, user_id, definition, sid, None, dashboard_id=dashboard_id, tokens=tokens, marking_ceiling=marking_ceiling)
            data = _without_links(read)
        except Exception:  # one source that cannot be read leaves the others
            logger.warning("Published dashboard %s: source %r could not be read", dashboard_id, sid[:60], exc_info=True)
            data = dict(_NOT_KEPT)
        size += len(json.dumps(data, default=str))
        sources[sid] = data if size <= MAX_SNAPSHOT_BYTES else dict(_NOT_KEPT)
    widgets: dict[str, dict] = {}
    for vid, viz in (definition.get("visualizations") or {}).items():
        sid = (viz.get("dataSources") or {}).get("primary")
        if sid in sources:
            widgets[vid] = sources[sid]
    return {"widgets": widgets, "sources": sources}


def _stale(dashboard: CustomDashboard, link: CustomDashboardPublicLink, now: datetime) -> bool:
    if link.snapshot is None or link.snapshot_at is None or link.snapshot_revision != dashboard.revision:
        return True
    return link.snapshot_at < now - timedelta(minutes=max(link.refresh_minutes or DEFAULT_REFRESH_MINUTES, REFRESH_CHOICES[0]))


async def current_snapshot(
    db: AsyncSession, dashboard: CustomDashboard, link: CustomDashboardPublicLink, publisher: User, *, force: bool = False
) -> tuple[dict, datetime | None]:
    """The link's snapshot, read again first when stale (or *force*) and no other request is reading it.

    One refresh per link at a time (a Redis lock, ``REFRESH_LOCK_SECONDS``);
    a request that does not get it, or a Redis that does not answer, is served
    the snapshot as it is - never a second read, never a read without the
    lock. Commits the new snapshot.
    """
    from app.core.redis import redis_client
    from app.services.dashboard_definition import upgrade

    now = datetime.now(UTC)
    kept = (link.snapshot or {"widgets": {}, "sources": {}}, link.snapshot_at)
    if not force and not _stale(dashboard, link, now):
        return kept
    lock = f"dashboard-public-snapshot:{link.id}"
    try:
        client = await redis_client()
        if not await client.set(lock, "1", nx=True, ex=REFRESH_LOCK_SECONDS):
            return kept
    except Exception:
        logger.warning("Published dashboard %s: the refresh lock could not be taken; serving the snapshot kept", dashboard.id)
        return kept
    try:
        definition = upgrade(dashboard.spec)
        snapshot = await read_snapshot(db, definition, dashboard.id, publisher.id, tuple(link.marking_ceiling or ()))
        link.snapshot, link.snapshot_at, link.snapshot_revision = snapshot, now, dashboard.revision
        await db.commit()
        return snapshot, now
    finally:
        try:
            await client.delete(lock)
        except Exception:  # the lock expires on its own
            logger.warning("Published dashboard %s: the refresh lock could not be released", dashboard.id)


def public_definition(definition: dict, snapshot: dict) -> dict:
    """The definition as a visitor receives it: what draws, and nothing about where the data comes from."""
    sources: dict[str, dict] = {}
    for sid, source in (definition.get("dataSources") or {}).items():
        data = (snapshot.get("sources") or {}).get(sid) or {}
        shape = data.get("shape") if data.get("status") == "ok" else None
        entry: dict[str, Any] = {"type": "static", "options": {"shape": shape or "number", "data": {}}}
        if source.get("name"):
            entry["name"] = source["name"]
        sources[sid] = entry
    visualizations = {}
    for vid, viz in (definition.get("visualizations") or {}).items():
        clean = {k: v for k, v in viz.items() if k != "eventHandlers"}
        options = clean.get("options")
        if isinstance(options, dict) and "link" in options:
            clean["options"] = {k: v for k, v in options.items() if k != "link"}
        visualizations[vid] = clean
    layout = dict(definition.get("layout") or {})
    layout["globalInputs"] = []
    layout["layoutDefinitions"] = {
        lid: {**body, "structure": [e for e in body.get("structure") or [] if e.get("type", "block") != "input"]}
        for lid, body in (layout.get("layoutDefinitions") or {}).items()
    }
    expressions = definition.get("expressions") or {}
    return {
        **{k: v for k, v in definition.items() if k in ("version", "title", "description", "theme", "defaults", "applicationProperties")},
        "dataSources": sources,
        "visualizations": visualizations,
        "inputs": {},
        "layout": layout,
        "expressions": {"conditions": expressions.get("conditions") or {}},
    }


async def public_payload(db: AsyncSession, dashboard: CustomDashboard, link: CustomDashboardPublicLink, publisher: User) -> dict:
    """What a visit receives: the title, the definition reduced to what draws, the snapshot and when it was read."""
    from app.services.dashboard_definition import upgrade

    snapshot, read_at = await current_snapshot(db, dashboard, link, publisher)
    return {
        "title": dashboard.title,
        "definition": public_definition(upgrade(dashboard.spec), snapshot),
        "snapshot": snapshot,
        "snapshot_at": read_at.isoformat() if read_at else None,
    }


def audit_details(outcome: str) -> dict[str, Any]:
    return {"public_link": outcome, "sharing_changed": True}
