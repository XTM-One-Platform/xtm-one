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

"""Knowledge Base sync service – imports content from external sources.

Supports:
  - Notion: pages and databases (via Notion API)
  - Microsoft OneDrive / SharePoint: files from a folder (via Graph API)
  - Google Drive: files from a folder (via Drive API)
  - GitHub: repository files (via GitHub API)
  - Drata: compliance policies (via Drata API v2)
  - Website: web crawling via trafilatura

Sync strategy (full-refresh):
  1. Fetch ALL files/pages from the external source
  2. Upsert documents: update if content changed, insert if new
  3. Delete stale documents that no longer exist in the source
  4. Enqueue changed/new documents for embedding
  5. Recount kb.document_count from the DB
"""

import asyncio
import functools
import hashlib
import itertools
import logging
import re
import secrets
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.egress import EgressRoute, egress_client
from app.core.security import decrypt_value
from app.core.utils import format_exception_message
from app.core.utils import normalize_github_repo as _normalize_github_repo
from app.services.integration_credentials import oauth_credentials_usable_clause
from app.services.integration_user_credentials import (
    connected_for_clause,
    integration_of,
    is_per_user,
    resolve_token_holder,
)
from app.services.kb_helpers import (
    WEBSITE_ALL_PERMANENT_FAILURES_MARKER,
    bump_kb_revision,
    cancel_document_jobs_after_commit,
    enqueue_documents,
    reset_changed_document,
)
from app.services.vault import reveal_field, reveal_json_field

# ── Public exceptions ────────────────────────────────────────────────────────


class KBSyncConfigError(ValueError):
    """Raised when a KB sync source has a permanent configuration error.

    Unlike transient failures (e.g. network timeouts, rate limits), a
    configuration error will never succeed without the user fixing the source
    settings. The scheduler catches this exception to disable the source on
    the *first* occurrence and alert the owner, instead of retrying
    indefinitely on a known-broken configuration.
    """


class NotionSyncTotalFailureError(RuntimeError):
    """Raised when EVERY queued page of a Notion sync failed.

    Distinguishes the "all page references are stale or inaccessible" total
    failure from unrelated runtime errors so callers can handle it precisely:
    the scheduler lets it bubble into the failure-count / quarantine path,
    and the manual sync API maps it to a clean HTTP 502 with the aggregated
    per-page errors. Subclasses ``RuntimeError`` for backward compatibility.
    """


# ── Sync limits ──────────────────────────────────────────────────────────────
_MIN_DOCUMENT_CONTENT_LENGTH = 20
_NOTION_MAX_BLOCK_DEPTH = 5
_MAX_NOTION_PAGES_SYNC = 500
_MAX_DRIVE_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_GITHUB_FILE_SIZE_BYTES = 500_000  # 500 KB
_DRATA_PAGE_SIZE = 50
_MAX_FILENAME_LENGTH = 150
_DEFAULT_MAX_WEBSITE_PAGES = 50
_MAX_WEBSITE_PAGES_CAP = 500
_MAX_WEBSITE_DEPTH_CAP = 5
_NOTION_REQUESTS_PER_SECOND = 3.0
_NOTION_MAX_CONCURRENCY = 3
_NOTION_RETRY_ATTEMPTS = 5
_NOTION_RETRY_BASE_DELAY = 1.0
# Stale child-database tracking (404s): after this many consecutive 404
# failures for a Notion child-database discovered during a recursive sync, the
# child database is marked stale and silently skipped on subsequent runs.  A
# single actionable warning is emitted when the stale threshold is first
# reached (instead of re-logging the same 404 on every sync cycle).  The
# database is re-tried automatically after _NOTION_CHILD_DB_RECHECK_DAYS to
# detect any recovery (e.g. the Notion structure was restored or the
# integration reconnected).  State lives in ``config["stale_child_dbs"]``
# ({db_id: {count, first_failed_at, last_failed_at}}), superseding the legacy
# one-shot ``config["skipped_child_db_ids"]`` list (migrated on load).
_NOTION_CHILD_DB_STALE_THRESHOLD = 3
_NOTION_CHILD_DB_RECHECK_DAYS = 7
# Entries whose last failure is older than this are dropped from the persisted
# tracking dict entirely.  A child database that keeps failing is re-attempted
# (and its ``last_failed_at`` refreshed) at least once per recheck window, so
# an entry this old belongs to a block that no longer appears in the synced
# pages at all — without pruning, the sync-source config would grow without
# bound as Notion structures come and go.
_NOTION_CHILD_DB_PRUNE_DAYS = 60
# Number of consecutive failing sync cycles before a child database is
# quarantined (skipped on subsequent syncs) for NON-404 failures — e.g. an
# HTTP 400 from a stale data_source reference after the Notion API migration.
# 404s take the counted ``stale_child_dbs`` path above; non-404
# failures increment a per-source counter stored in the sync source's
# ``config["stale_child_databases"]`` dict ({db_id: count}).  Below the
# threshold a successful query removes the entry (transient failures
# self-heal).  Once quarantined, the database is re-probed once every
# ``_NOTION_STALE_CHILD_DB_RECHECK_EVERY`` skipped runs so a
# repaired database recovers automatically; a force re-sync still clears
# the tracking dict immediately.
_NOTION_STALE_CHILD_DB_THRESHOLD = 3
# Periodic recovery probe cadence for the NON-404 quarantine above: every
# Nth quarantined sync run the child database is queried again instead of
# skipped.  The counter keeps incrementing on skipped runs purely to drive
# this modulo — there are no timestamps in the {db_id: count} dict, so the
# cadence is expressed in runs (daily sync = weekly probe), mirroring the
# 404 path's ``_NOTION_CHILD_DB_RECHECK_DAYS`` recovery cycle.  A probe
# that succeeds clears the entry (existing success path); a probe that
# fails keeps counting and waits another N runs.
_NOTION_STALE_CHILD_DB_RECHECK_EVERY = 7
_WEBSITE_REQUEST_DELAY = 0.5  # base seconds between requests to avoid rate limiting
_WEBSITE_MAX_REQUEST_DELAY = 8.0  # cap for the adaptive inter-request delay
_WEBSITE_THROTTLE_BACKOFF = 1.5  # grow the inter-request delay by this on a transient failure
_WEBSITE_DELAY_DECAY = 0.9  # relax the inter-request delay by this after a clean fetch
_WEBSITE_RETRY_ATTEMPTS = 3
_WEBSITE_RETRY_BASE_DELAY = 2.0  # seconds — doubles on each retry
# Abort the crawl after this many consecutive *permanent* failures (404/403/
# 401 …): a broken base URL or a site we're not allowed to read. Transient
# failures (429/5xx/network) do NOT count here — they're absorbed by adaptive
# throttling.
_WEBSITE_CONSECUTIVE_PERMANENT_ABORT = 5
# Safety valve: give up only if the site keeps failing transiently *in a row*
# even after we've slowed down, so an aggressively rate-limited or hard-down
# site can't silently burn the whole sync window. Reset on any success, so a
# site that rate-limits in bursts is still crawled to completion.
_WEBSITE_CONSECUTIVE_TRANSIENT_ABORT = 12
# Detects an explicit URL scheme ("https://", "ftp://", …) at the START of a
# candidate website-source URL (RFC 3986 scheme = ALPHA *(ALPHA / DIGIT / "+"
# / "-" / ".")). A plain substring check ("://" in url) would misfire on
# schemeless URLs whose query string embeds another URL
# (e.g. "example.com/redirect?to=https://x.com") and leave them broken.
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Detects hrefs that were MEANT to be absolute URLs but carry a mangled
# scheme separator (e.g. "https;//cursor.com" — note the semicolon).
# ``urljoin`` treats such a value as a *relative path* and produces nonsense
# like "https://example.org/page/https;/cursor.com", which the crawler then
# fetches and 404s on. Matched hrefs are skipped during link extraction.
_MALFORMED_HREF_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*[;,]/")
# Same garbage arriving pre-joined: a path SEGMENT that is exactly a scheme
# token with a trailing ";" or ":" (e.g. ".../https;/cursor.com").
_MALFORMED_PATH_SEGMENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*[;:]$")


def _page_hrefs(html: str) -> list[str]:
    """The non-empty ``href`` values of a page's anchors, stripped, in order.

    A full HTML parse, CPU-bound on a large page: the website crawl runs it on
    the parse executor (``app.core.parse_offload.run_parse``), never inline on
    the event loop, where it stalled the web process for seconds.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    hrefs: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if href:
            hrefs.append(href)
    return hrefs


def _doubled_path_segments(path: str) -> Counter[str]:
    """Count the doubled-pair occurrences per (lowercased) segment in *path*.

    A segment counts as doubled when it occurs twice in a row (e.g. ``docs``
    in ``/docs/docs/intro``).  Used to budget the doubled pairs that are part
    of a website source's own base URL — some sites legitimately contain
    them (e.g. a GitHub repo whose org and repo share a name, like
    ``github.com/grafana/grafana``) — so the crawler guard below only skips
    doubled pairs *introduced* during link resolution.  Counting
    occurrences (rather than collecting a name set) means a resolved link
    that re-doubles the same segment a second time (e.g.
    ``/grafana/grafana/tree/main/grafana/grafana/x``) still trips the guard.
    """
    segs = [s.lower() for s in path.split("/") if s]
    return Counter(a for a, b in itertools.pairwise(segs) if a == b)


def _has_doubled_path_segment(
    path: str, allowed: Mapping[str, int] | None = None
) -> bool:
    """Return ``True`` if *path* has two consecutive identical segments.

    A doubled segment (e.g. ``/.github/.github/LABELS.md``) is typically
    produced when a page whose path already ends with a segment is joined
    with a relative href that re-states the same segment — yielding a URL
    that almost always 404s.  This is a heuristic: consecutive
    duplicates can be valid on some sites, so *allowed* (per-segment counts
    of doubled pairs already present in the source's base URL, from
    :func:`_doubled_path_segments`) grants each segment a budget: only
    occurrences *beyond* the base URL's own count trip the guard.
    Case-insensitive so ``/Docs/docs/`` is also caught.
    """
    doubled = _doubled_path_segments(path)
    if not allowed:
        return bool(doubled)
    return any(count > allowed.get(seg, 0) for seg, count in doubled.items())


_GITHUB_FILE_FETCH_DELAY = 0.1  # seconds between per-file API calls
# Bounded retry for per-file GitHub content fetches: transient 5xx (e.g.
# "502 Bad Gateway" blips from api.github.com), 429 and network errors are
# retried with backoff via _fetch_with_retry instead of failing the file
# outright and leaving a gap in the KB until the next full sync.
_GITHUB_FILE_RETRY_ATTEMPTS = 3
_GITHUB_FILE_RETRY_BASE_DELAY = 1.0  # seconds — doubles on each retry
_EMBEDDING_DELETE_BATCH_SIZE = 50
_EMBEDDING_DELETE_MAX_RETRIES = 3
_EMBEDDING_DELETE_LOCK_SCOPE = "kb_sync_embeddings_delete"

# Path segments that almost never contain useful content.
# Checked against every segment of the URL path (case-insensitive).
_SKIP_PATH_SEGMENTS = frozenset(
    {
        "login",
        "signin",
        "signup",
        "register",
        "logout",
        "settings",
        "admin",
        "account",
        "cart",
        "checkout",
        "search",
        "marketplace",
        "billing",
        "partners",
        "community",
        "notifications",
        "explore",
        "trending",
        "sponsors",
        "codespaces",
        "discussions",
        "pulls",
        "stars",
        "stargazers",
        "followers",
        "following",
        "achievements",
        "404",
        "actions",
        "security",
        "projects",
        "packages",
        "forks",
        "watchers",
        "graphs",
        "network",
        "deployments",
        "issues",
        "labels",
        "milestones",
        "releases",
        "tags",
        "activity",
        "commits",
        "branches",
        "compare",
        "pulse",
        "people",
        "contributors",
    }
)
from app.integrations.http import PLATFORM_USER_AGENT
from app.integrations.providers.github import GITHUB_API
from app.integrations.providers.google import DRIVE_API
from app.integrations.providers.microsoft import GRAPH_API
from app.integrations.providers.notion import (
    NOTION_API,
    NOTION_VERSION,
    _rich_text_to_plain,
)
from app.models.embedding import Embedding
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.knowledge_base import KBSyncSource, KnowledgeBase, KnowledgeDocument

logger = logging.getLogger(__name__)


async def _apply_sync_statement_timeout(
    db: AsyncSession, timeout_secs: int | None
) -> None:
    """Lift the per-transaction ``statement_timeout`` for a KB-sync session.

    Every pooled connection carries the global 30s ``statement_timeout`` set in
    ``core/database.py``. Large-content INSERTs (25K+ char website pages) and
    bulk embedding / stale-cleanup DELETEs on ``knowledge_documents`` routinely
    exceed that, so a session doing sync writes must raise the cap to the
    configured ``kb_sync_timeout_secs``.

    This matters most for :func:`sync_website`, which commits each crawled page
    in its own short-lived session (and runs the stale cleanup in another)
    instead of the caller's already-elevated ``sync_db`` transaction — those
    fresh sessions would otherwise fall back to the 30s pool default and cancel
    every large INSERT/DELETE with SQLSTATE 57014.

    ``set_config(..., is_local => true)`` scopes the override to the current
    transaction so it auto-resets when the transaction ends — the elevated
    timeout never leaks onto the pooled connection for unrelated queries.

    A ``None`` / non-positive ``timeout_secs`` is a no-op (keeps the pool
    default) so callers that don't supply one are unaffected.
    """
    if not timeout_secs or timeout_secs <= 0:
        return
    statement_timeout_ms = min(max(1, int(timeout_secs)) * 1000, 2_147_483_647)
    conn = await db.connection()
    await conn.execute(
        text("SELECT set_config('statement_timeout', :statement_timeout_ms, true)"),
        {"statement_timeout_ms": str(statement_timeout_ms)},
    )


def _is_retryable_embedding_delete_error(exc: Exception) -> bool:
    """Return True for transient DB errors that are safe to retry."""
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if sqlstate in {"40P01", "40001"}:
        return True
    msg = str(exc).lower()
    return "deadlock" in msg or "serialization" in msg


async def _acquire_embedding_delete_lock(db: AsyncSession, kb_id: uuid.UUID) -> None:
    """Serialize embedding deletes per knowledge base within a transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope), hashtext(:kb_key))"),
        {"scope": _EMBEDDING_DELETE_LOCK_SCOPE, "kb_key": str(kb_id)},
    )


async def _delete_embeddings_for_source_ids(
    db: AsyncSession,
    kb_id: uuid.UUID,
    source_ids: list[str],
) -> int:
    """Delete embedding rows for source IDs using deterministic, retried batches.

    Concurrency design (per the ``embeddings`` 40P01 deadlocks):

    * The advisory lock is acquired **once** before any DELETE so concurrent
      KB-sync workers targeting the same knowledge base serialise their
      embedding deletes instead of racing on overlapping ``source_id`` ranges.
    * Each batch DELETE runs inside its own ``begin_nested()`` SAVEPOINT so a
      transient deadlock/serialization failure can be retried without
      poisoning the outer transaction (savepoint rollback also clears the
      "transaction aborted" state that 40P01 / 40001 normally leave behind).
    * The lock itself is acquired outside the per-batch savepoint, so it
      survives savepoint releases for the duration of the outer transaction
      it was opened in. If the *caller's* savepoint rolls back (e.g. an
      exception escapes ``_upsert_document``), the advisory lock is released
      with it — which is the desired behaviour because the whole logical
      operation is then aborted.
    * Source IDs are normalised with ``sorted(set(...))`` so concurrent
      callers acquire row locks in the same global order, eliminating the
      lock-order inversion that caused the original deadlock.
    """
    ordered_source_ids = sorted(set(source_ids))
    if not ordered_source_ids:
        return 0

    await _acquire_embedding_delete_lock(db, kb_id)
    deleted = 0
    for start in range(0, len(ordered_source_ids), _EMBEDDING_DELETE_BATCH_SIZE):
        batch = ordered_source_ids[start : start + _EMBEDDING_DELETE_BATCH_SIZE]
        for attempt in range(1, _EMBEDDING_DELETE_MAX_RETRIES + 1):
            try:
                async with db.begin_nested():
                    result = await db.execute(
                        delete(Embedding).where(
                            Embedding.knowledge_base_id == kb_id,
                            Embedding.source_id.in_(batch),
                        )
                    )
                deleted += int(getattr(result, "rowcount", 0) or 0)
                break
            except Exception as exc:
                if (
                    attempt < _EMBEDDING_DELETE_MAX_RETRIES
                    and _is_retryable_embedding_delete_error(exc)
                ):
                    delay = 0.05 * (2 ** (attempt - 1))
                    logger.warning(
                        "Retryable DB error deleting embedding batch for KB %s "
                        "(attempt %d/%d), retrying in %.2fs: %s",
                        kb_id,
                        attempt,
                        _EMBEDDING_DELETE_MAX_RETRIES,
                        delay,
                        format_exception_message(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
    return deleted


async def _get_integration_token(
    db: AsyncSession,
    user_id: uuid.UUID | str,
    provider: str,
    *,
    integration_id: str | None = None,
) -> str:
    """Get the access token of the integration :func:`resolve_sync_integration` picks."""
    integration = await resolve_sync_integration(
        db, user_id, provider, integration_id=integration_id
    )
    if integration.access_token_enc:
        return decrypt_value(integration.access_token_enc)
    if integration.api_key_enc:
        return await reveal_field(integration, "api_key_enc") or ""
    raise ValueError(
        f"{provider} integration has no access token. Re-authorize it via OAuth."
    )


async def sync_egress_route(
    db: AsyncSession,
    user_id: uuid.UUID | str,
    provider: str,
    *,
    integration_id: str | None = None,
) -> EgressRoute | None:
    """The route of the integration a sync of *provider* uses.

    A sync source bound to an integration routed through a relay group reads
    its documents through that group. ``None`` (direct) when no integration
    resolves: the sync itself then fails with the reason.
    """
    from app.services.relays import integration_egress_route

    try:
        integration = await resolve_sync_integration(
            db, user_id, provider, integration_id=integration_id
        )
    except ValueError:
        return None
    return await integration_egress_route(db, integration_of(integration))


async def resolve_sync_integration(
    db: AsyncSession,
    user_id: uuid.UUID | str,
    provider: str,
    *,
    integration_id: str | None = None,
) -> Integration | IntegrationUserCredential:
    """The connected integration a sync of *provider* uses (or, on one whose
    people each connect their own account, the user's own credential).

    Resolution order:
      0. If *integration_id* is provided, use that specific integration, as
         bound: who may use it was decided when it was bound.
      1. The user's own connected integration for *provider*.
      2. Any company-managed integration for *provider* the user may see
         (``access_restricted`` narrows it to its grantees; the owner
         is preserved on the row).
    """
    integration = None

    # 0. Explicit integration binding (set when creating the sync source)
    if integration_id:
        result = await db.execute(
            select(Integration)
            .where(
                Integration.id == uuid.UUID(str(integration_id)),
                Integration.provider == provider,
                # A person's own account counts for its person.
                connected_for_clause(user_id),
            )
            .limit(1)
        )
        integration = result.scalar_one_or_none()
        if not integration:
            raise ValueError(
                f"Bound {provider} integration ({integration_id}) is not connected. "
                f"Check it in Integrations or re-authorize."
            )
        # An integration whose people each connect their own account syncs
        # with the source owner's own, never with its holder's.
        if is_per_user(integration):
            holder = await resolve_token_holder(db, integration, user_id)
            if holder is None:
                raise ValueError(
                    f"The bound {provider} integration '{integration.name}' acts with "
                    "each person's own account, and this source's owner has not "
                    "connected theirs. Connect it from the integration's page."
                )
            return holder

    # 1. Try user's own *private* integration. Company-managed rows now
    #    preserve ``user_id`` (typically the admin), so this must explicitly
    #    exclude them — otherwise an admin-owned company-managed integration
    #    would be returned here ahead of the user's private connection,
    #    collapsing the "own first, shared fallback" order into shared-first.
    if not integration:
        result = await db.execute(
            select(Integration)
            .where(
                Integration.user_id == uuid.UUID(str(user_id)),
                Integration.is_company_managed.is_(False),
                Integration.provider == provider,
                Integration.status == "connected",
            )
            .limit(1)
        )
        integration = result.scalar_one_or_none()

    # 2. Fall back to company-managed integration. OAuth tokens are personal
    #    to whoever connected them: a company-managed OAuth row is
    #    only usable here by its own owner, unless the owner explicitly opted
    #    in via ``oauth_share_acknowledged`` — the same ownership gate
    #    ``shadow_invisible_bindings`` applies to fixed tool bindings.
    if not integration:
        from app.api.v1.helpers import integration_visible_filter

        visible = await integration_visible_filter(db, user_id)
        result = await db.execute(
            select(Integration)
            .where(
                # access_restricted: honoured through ``visible``.
                Integration.is_company_managed.is_(True),
                visible,
                Integration.provider == provider,
                Integration.status == "connected",
                oauth_credentials_usable_clause(uuid.UUID(str(user_id))),
            )
            .limit(1)
        )
        integration = result.scalar_one_or_none()

    if not integration:
        raise ValueError(
            f"No connected {provider} integration found. "
            f"Connect a personal one in Integrations, or ask an admin to set up a company-managed {provider} integration."
        )

    logger.info(
        "KB sync using %s integration '%s' (id=%s, auth=%s, owner=%s)",
        provider,
        integration.name,
        integration.id,
        integration.auth_type,
        (
            "company-managed"
            if integration.is_company_managed
            else str(integration.user_id)
        ),
    )
    return integration


async def _get_drata_integration(
    db: AsyncSession, user_id: uuid.UUID | str, integration_id: str | None = None
) -> Integration | None:
    """Resolve the Drata integration object (for reading extra_config).

    The bound one as bound, else the user's own, else a company-managed
    one the user may see.
    """
    if integration_id:
        result = await db.execute(
            select(Integration)
            .where(
                Integration.id == uuid.UUID(str(integration_id)),
                Integration.provider == "drata",
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    # User's own *private* Drata integration first. Exclude company-managed
    # rows (which now preserve ``user_id``) so an admin-owned shared Drata
    # connection doesn't pre-empt the user's private one — the shared row is
    # the explicit fallback below.
    result = await db.execute(
        select(Integration)
        .where(
            Integration.user_id == uuid.UUID(str(user_id)),
            Integration.is_company_managed.is_(False),
            Integration.provider == "drata",
        )
        .limit(1)
    )
    integ = result.scalar_one_or_none()
    if integ:
        return integ

    from app.api.v1.helpers import integration_visible_filter

    visible = await integration_visible_filter(db, user_id)
    result = await db.execute(
        select(Integration)
        .where(
            # access_restricted: honoured through ``visible``.
            Integration.is_company_managed.is_(True),
            visible,
            Integration.provider == "drata",
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


def _sync_source_uuid(sync_source_id: uuid.UUID | str | None) -> uuid.UUID | None:
    if sync_source_id is None or isinstance(sync_source_id, uuid.UUID):
        return sync_source_id
    return uuid.UUID(str(sync_source_id))


def _unowned_sync_document() -> ColumnElement[bool]:
    """A document no sync source of its knowledge base owns.

    Its ``sync_source_id`` is NULL (an upload, a one-shot import, a document
    synced before the column existed) or names no source of the document's
    knowledge base any more (the source was removed).
    """
    return or_(
        KnowledgeDocument.sync_source_id.is_(None),
        ~exists().where(
            KBSyncSource.id == KnowledgeDocument.sync_source_id,
            KBSyncSource.knowledge_base_id == KnowledgeDocument.knowledge_base_id,
        ),
    )


def owned_sync_documents(
    source_type: str, sync_source_id: uuid.UUID | str | None
) -> ColumnElement[bool]:
    """The documents a sync of *source_type* owns in its knowledge base.

    The ONE rule for what a sync may replace or delete: the documents its own
    sync source imported, plus the unowned documents of its type. Another
    source's documents are never in it, so two sources of the same type in one
    knowledge base (two Notion pages, two repositories) no longer replace each
    other's documents on every run. A sync without a source (the one-shot
    import endpoints) owns only the unowned documents of its type. Combine it
    with the knowledge base filter.
    """
    unowned = and_(KnowledgeDocument.source_type == source_type, _unowned_sync_document())
    source_uuid = _sync_source_uuid(sync_source_id)
    if source_uuid is None:
        return unowned
    return or_(KnowledgeDocument.sync_source_id == source_uuid, unowned)


async def _sync_source_exists(
    db: AsyncSession, kb: KnowledgeBase, sync_source_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(KBSyncSource.id).where(
            KBSyncSource.id == sync_source_id,
            KBSyncSource.knowledge_base_id == kb.id,
        )
    )
    return result.scalar_one_or_none() is not None


async def _upsert_document(
    db: AsyncSession,
    kb: KnowledgeBase,
    filename: str,
    content: str,
    source_type: str,
    source_url: str | None = None,
    *,
    sync_source_id: uuid.UUID | str | None = None,
) -> tuple[KnowledgeDocument | None, bool]:
    """Upsert a document: update if content changed, insert if new.

    ``sync_source_id`` is the :class:`KBSyncSource` behind the sync (None for
    a one-shot import): a new document records it, and an unowned document
    the sync finds is claimed by it.

    Returns (document, needs_embedding):
      - (doc, True)  if the doc was created or its content changed (needs re-embedding)
      - (doc, False) if the doc exists with the same content (no re-embedding)
      - (None, False) if content is too short to be useful
    """
    if not content or len(content.strip()) < _MIN_DOCUMENT_CONTENT_LENGTH:
        return None, False

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    source_uuid = _sync_source_uuid(sync_source_id)

    # Look up by source_url (stable identifier) or by filename+source_type.
    # Use .limit(1) to avoid MultipleResultsFound when concurrent syncs
    # have created duplicate documents for the same file.
    existing: KnowledgeDocument | None = None
    if source_url:
        # The URL names one remote item: two sources reaching the same page
        # share its document rather than holding two copies of it.
        result = await db.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.source_url == source_url,
            )
            .limit(1)
        )
        existing = result.scalars().first()

    if not existing:
        # A filename names nothing outside its source: only a document this
        # sync owns may be matched by it, never another source's same-named
        # file. An unowned document whose URL names another remote item
        # belongs to that item: claiming it would hand it to whichever source
        # synced last, on every run.
        by_filename = [
            KnowledgeDocument.knowledge_base_id == kb.id,
            KnowledgeDocument.filename == filename,
            owned_sync_documents(source_type, source_uuid),
        ]
        if source_url:
            no_other_item = KnowledgeDocument.source_url.is_(None)
            if source_uuid is not None:
                no_other_item = or_(no_other_item, KnowledgeDocument.sync_source_id == source_uuid)
            by_filename.append(no_other_item)
        result = await db.execute(select(KnowledgeDocument).where(*by_filename).limit(1))
        existing = result.scalars().first()

    if (
        existing
        and source_uuid is not None
        and existing.sync_source_id != source_uuid
        and (
            existing.sync_source_id is None
            or not await _sync_source_exists(db, kb, existing.sync_source_id)
        )
    ):
        # Claim an unowned document: from now on only this source's sweep
        # may delete it.
        existing.sync_source_id = source_uuid

    if existing and source_url and existing.source_url != source_url:
        # Matched by filename: the document now names the item this sync
        # reached, so the next run finds it by URL.
        existing.source_url = source_url

    if existing:
        if existing.content_hash == content_hash:
            # Content unchanged — no work needed
            return existing, False
        # Content changed — update and re-embed.
        # Use no_autoflush so the DELETE below doesn't trigger a premature
        # autoflush of the dirty UPDATE (which can hit statement_timeout on
        # large content columns).
        with db.no_autoflush:
            existing.content = content
            existing.content_hash = content_hash
            existing.filename = filename
            await _delete_embeddings_for_source_ids(db, kb.id, [str(existing.id)])
            await reset_changed_document(db, existing, vectors_deleted=True)
        await db.flush()
        return existing, True

    # New document
    doc = KnowledgeDocument(
        knowledge_base_id=kb.id,
        filename=filename,
        source_type=source_type,
        source_url=source_url,
        sync_source_id=source_uuid,
        content=content,
        content_hash=content_hash,
        status="pending",
    )
    db.add(doc)
    await db.flush()
    return doc, True


_STALE_DOC_BATCH = 500


async def _delete_stale_documents(
    db: AsyncSession,
    kb: KnowledgeBase,
    source_type: str,
    seen_doc_ids: set[str],
    *,
    sync_source_id: uuid.UUID | str | None = None,
) -> int:
    """Delete documents from a source that were not seen in the latest sync.

    Only the documents the sync owns are candidates
    (:func:`owned_sync_documents`): another source's documents of the same
    type are never this sync's to delete.

    Two-phase, so the queued jobs are cancelled once for the whole stale set
    rather than once per delete batch:

    1. **Collect** — paginate the docs table and accumulate the full set
       of stale ``KnowledgeDocument`` ids in memory. Pagination here is
       only an SQL-side guard against an unbounded ``SELECT`` returning
       a giant result set; the ids themselves (UUIDs, ~36 bytes each)
       are cheap to keep — even 1 M stale ids is ~36 MB, well below the
       memory headroom of a sync operation that already holds DB and
       HTTP sessions open.
    2. **Delete + cancel** — re-iterate the collected ids in fixed-size
       chunks for the embedding-delete + ``KnowledgeDocument`` delete
       (batched to keep the SQL ``WHERE … IN (…)`` predicate small), then
       hand the full id set to ``cancel_document_jobs_after_commit``: the
       caller commits, and a sync that fails before its commit keeps both
       the documents and their jobs.

    Returns the number of deleted documents.
    """
    # ── Phase 1: collect all stale ids ──────────────────────────────
    stale_ids: list[uuid.UUID] = []
    offset = 0
    while True:
        result = await db.execute(
            select(KnowledgeDocument.id)
            .where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                owned_sync_documents(source_type, sync_source_id),
            )
            .limit(_STALE_DOC_BATCH)
            .offset(offset)
        )
        batch_ids = result.scalars().all()
        if not batch_ids:
            break
        stale_ids.extend(did for did in batch_ids if str(did) not in seen_doc_ids)
        if len(batch_ids) < _STALE_DOC_BATCH:
            break
        offset += _STALE_DOC_BATCH

    if not stale_ids:
        return 0

    # ── Phase 2: batched deletes, their jobs cancelled once committed ──
    stale_id_strings_all = [str(d) for d in stale_ids]
    deleted = 0
    for start in range(0, len(stale_ids), _STALE_DOC_BATCH):
        chunk = stale_ids[start : start + _STALE_DOC_BATCH]
        chunk_id_strings = stale_id_strings_all[start : start + _STALE_DOC_BATCH]
        await _delete_embeddings_for_source_ids(
            db,
            kb.id,
            chunk_id_strings,
        )
        await db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(chunk))
        )
        deleted += len(chunk)
    cancel_document_jobs_after_commit(db, stale_id_strings_all)
    return deleted


async def _recount_kb_stats(
    db: AsyncSession, kb: KnowledgeBase, *, content_changed: bool
) -> None:
    """Recount document_count and chunk_count from the actual DB records.

    *content_changed* says whether the caller's transaction added, changed or
    deleted a document of *kb*: every sync passes the documents
    ``_upsert_document`` reported as needing embedding and the count
    ``_delete_stale_documents`` returned (``bool(embed_doc_ids or deleted)``),
    and a sync that commits page by page passes only what this transaction
    deleted. When true, the KB's content revision moves with the recount.

    Note: ``document_processor._recount_kb_stats`` is a related helper that
    only recounts ``chunk_count`` in a *separate* session to avoid deadlocks
    during concurrent embedding writes.  This variant updates both counters on
    the caller's session because sync operations are serialised per KB.
    """
    doc_count = await db.execute(
        select(func.count())
        .select_from(KnowledgeDocument)
        .where(KnowledgeDocument.knowledge_base_id == kb.id)
    )
    kb.document_count = doc_count.scalar() or 0

    chunk_count = await db.execute(
        select(func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0)).where(
            KnowledgeDocument.knowledge_base_id == kb.id
        )
    )
    kb.chunk_count = chunk_count.scalar() or 0

    # When this sync's transaction added, changed or deleted a document, move
    # the KB's content revision with it (the evaluation reuse fingerprint). A
    # sync that found everything as it was leaves it alone, so a scheduled
    # re-sync of an unchanged source keeps reused predictions valid.
    if content_changed:
        await bump_kb_revision(db, kb.id)


# ---------------------------------------------------------------------------
# Notion sync
# ---------------------------------------------------------------------------


def _parse_retry_after_seconds(retry_after: str | None) -> float:
    """Parse HTTP Retry-After header value into seconds."""
    if not retry_after:
        return 0.0
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            return 0.0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _is_notion_permanent_page_sync_error(exc: Exception, *, page_id: str | None = None) -> bool:
    """Return True when a Notion page fetch failed with a permanent page reference error.

    For explicit page-based sync sources, these errors indicate the configured
    page id is stale/broken and will keep failing until reconfigured.

    Matches:
    - HTTP 400/404/410 from GET /v1/pages/{id} — the page metadata fetch
      (stale references surface as 400 there too);
    - HTTP 404/410 from GET /v1/blocks/{page_id}/children — the top-level
      block-content fetch, only when *page_id* is provided.
      400s from the blocks endpoint are NOT matched: they signal
      request-level problems (bad cursor, malformed body), not a stale page.
      Nested-block 404s (different block IDs) are NOT treated as permanent
      page errors either.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status = exc.response.status_code
    if status not in {400, 404, 410}:
        return False
    path = exc.request.url.path.lower() if exc.request is not None else ""
    if path.startswith("/v1/pages/"):
        return True
    # A 404/410 from the top-level blocks children fetch for THIS page is
    # equally permanent — the page's content is inaccessible. Exact path
    # match so other block endpoints can never be misclassified.
    return bool(
        page_id
        and status in {404, 410}
        and path == f"/v1/blocks/{page_id.lower()}/children"
    )


class _NotionRequestController:
    """Throttle + retry wrapper for Notion API requests."""

    def __init__(
        self,
        *,
        max_concurrency: int = _NOTION_MAX_CONCURRENCY,
        requests_per_second: float = _NOTION_REQUESTS_PER_SECOND,
        retry_attempts: int = _NOTION_RETRY_ATTEMPTS,
        retry_base_delay: float = _NOTION_RETRY_BASE_DELAY,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._throttle_lock = asyncio.Lock()
        self._min_interval = (
            0.0
            if requests_per_second <= 0
            else 1.0 / max(0.001, float(requests_per_second))
        )
        self._next_request_at = 0.0
        self._retry_attempts = max(1, retry_attempts)
        self._retry_base_delay = max(0.0, retry_base_delay)

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        for attempt in range(self._retry_attempts):
            async with self._semaphore:
                if self._min_interval > 0:
                    async with self._throttle_lock:
                        now = asyncio.get_running_loop().time()
                        wait = max(0.0, self._next_request_at - now)
                        if wait > 0:
                            await asyncio.sleep(wait)
                            now = asyncio.get_running_loop().time()
                        self._next_request_at = now + self._min_interval

                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                )

            if response.status_code == 429 and attempt < self._retry_attempts - 1:
                retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After"))
                backoff = self._retry_base_delay * (2**attempt)
                wait = max(retry_after, backoff)
                logger.info(
                    "Notion sync: 429 for %s %s — retrying in %.2fs",
                    method,
                    url,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            if response.status_code >= 500 and attempt < self._retry_attempts - 1:
                wait = self._retry_base_delay * (2**attempt)
                logger.info(
                    "Notion sync: %d for %s %s — retrying in %.2fs",
                    response.status_code,
                    method,
                    url,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            return response

        return response


# ── Notion stale child-database helpers ─────────────────────────────────────


def _is_notion_child_db_stale(stale_child_dbs: dict, child_db_id: str) -> bool:
    """Return True if *child_db_id* should be skipped this run.

    A child database is considered stale (and therefore skipped) when:
    - It has failed with 404 at least ``_NOTION_CHILD_DB_STALE_THRESHOLD`` times
      in a row, AND
    - Less than ``_NOTION_CHILD_DB_RECHECK_DAYS`` days have elapsed since the
      last failure (so it gets a fresh attempt once a week to detect recovery).

    Corrupt persisted state (non-dict entry, non-int count, missing or
    unparseable timestamp — e.g. a hand-edited config) is treated as
    non-stale so the database is retried and the entry self-heals: a failed
    retry rewrites valid state via :func:`_record_child_db_404`, a successful
    one clears the entry.
    """
    entry = stale_child_dbs.get(child_db_id)
    if not isinstance(entry, dict) or not entry:
        return False
    count = entry.get("count", 0)
    if not isinstance(count, int) or count < _NOTION_CHILD_DB_STALE_THRESHOLD:
        return False
    last_failed_at_str = entry.get("last_failed_at")
    if not last_failed_at_str:
        # Missing timestamp (hand-edited / corrupt config): allow a retry so
        # the entry self-heals — a failed attempt re-records a valid
        # ``last_failed_at`` via _record_child_db_404, a successful one clears
        # the entry.  Returning True here would skip the database forever.
        return False
    try:
        last_failed_at = datetime.fromisoformat(last_failed_at_str)
        # If the last-failure timestamp is naive, assume UTC
        if last_failed_at.tzinfo is None:
            last_failed_at = last_failed_at.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last_failed_at) < timedelta(days=_NOTION_CHILD_DB_RECHECK_DAYS)
    except (ValueError, TypeError):
        return False  # unparseable timestamp → retry once to self-heal the entry


def _record_child_db_404(stale_child_dbs: dict, child_db_id: str) -> int:
    """Increment the consecutive-404 counter for *child_db_id*.

    Updates *stale_child_dbs* in-place with the new count and timestamps.
    Returns the new (post-increment) count.

    Defensive against corrupt persisted state: a non-dict entry or a
    non-int ``count`` (hand-edited config) restarts the streak from a fresh
    entry instead of raising and breaking the whole recursive sync.
    """
    now_str = datetime.now(UTC).isoformat()
    entry = stale_child_dbs.get(child_db_id)
    if not isinstance(entry, dict):
        entry = {}
    prior = entry.get("count", 0)
    if not isinstance(prior, int):
        prior = 0  # corrupt count — self-heal by restarting the streak
    count = prior + 1
    stale_child_dbs[child_db_id] = {
        "count": count,
        "first_failed_at": entry.get("first_failed_at", now_str),
        "last_failed_at": now_str,
    }
    return count


def _seed_404_counter_from_non404_failures(
    stale_child_dbs: dict,
    stale_child_databases: dict[str, int] | None,
    child_db_id: str,
) -> None:
    """Carry a prior non-404 failure streak into the 404 stale counter.

    When a child database that already failed several consecutive sync
    cycles with a non-404 error (e.g. HTTP 400 from a stale data_source
    reference before the Notion API endpoint migration) starts returning
    404, the 404 counter would otherwise restart from zero — requiring a
    full extra streak of 404 runs before the stale threshold is reached.
    Seeding the counter with the prior non-404 count preserves
    the consecutive-failure streak across the error-type transition.

    Only seeds when there is no *valid* 404 streak already tracked for
    *child_db_id* (so subsequent 404 runs are not double-counted).
    Mirroring :func:`_record_child_db_404`'s defensive handling, a
    corrupted persisted entry (non-dict, or a non-int / non-positive
    ``count``) is treated as absent — otherwise the carry-over would be
    silently lost exactly when the tracking state needs self-healing.
    Timestamps are left for :func:`_record_child_db_404` to fill in.

    The seeded count is clamped to ``_NOTION_CHILD_DB_STALE_THRESHOLD - 1``
    so the increment in :func:`_record_child_db_404` lands exactly on the
    threshold and the one-time "marking as stale" warning fires — an
    oversized carried count (the non-404 quarantine threshold raised above
    the 404 one, or a hand-edited config) must not overshoot into the
    "still stale after re-check" branch on the very first 404.
    """
    if stale_child_databases is None:
        return
    entry = stale_child_dbs.get(child_db_id)
    existing_count = entry.get("count") if isinstance(entry, dict) else None
    if isinstance(existing_count, int) and existing_count > 0:
        return  # ongoing 404 streak — do not re-seed
    prior_non404 = stale_child_databases.get(child_db_id, 0)
    if isinstance(prior_non404, int) and prior_non404 > 0:
        stale_child_dbs[child_db_id] = {
            "count": min(prior_non404, _NOTION_CHILD_DB_STALE_THRESHOLD - 1)
        }


def _prune_stale_child_db_entries(stale_child_dbs: dict) -> int:
    """Drop tracking entries whose last failure is older than the prune TTL.

    A child database that still exists in the synced pages is re-attempted at
    least once per ``_NOTION_CHILD_DB_RECHECK_DAYS`` window, refreshing its
    ``last_failed_at`` on every failure — so an entry that hasn't failed in
    ``_NOTION_CHILD_DB_PRUNE_DAYS`` days belongs to a block that disappeared
    from the workspace entirely.  Pruning keeps the persisted sync-source
    config from accumulating dead entries forever.  Entries with a missing or
    unparseable timestamp are also dropped (they cannot age out otherwise).

    Mutates *stale_child_dbs* in-place; returns the number of pruned entries.
    """
    cutoff = datetime.now(UTC) - timedelta(days=_NOTION_CHILD_DB_PRUNE_DAYS)
    to_prune: list[str] = []
    for child_db_id, entry in stale_child_dbs.items():
        if not isinstance(entry, dict):
            to_prune.append(child_db_id)  # corrupt entry — cannot age out
            continue
        last_failed_at_str = entry.get("last_failed_at")
        if not last_failed_at_str:
            to_prune.append(child_db_id)
            continue
        try:
            last_failed_at = datetime.fromisoformat(last_failed_at_str)
            if last_failed_at.tzinfo is None:
                last_failed_at = last_failed_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            to_prune.append(child_db_id)
            continue
        if last_failed_at < cutoff:
            to_prune.append(child_db_id)
    for child_db_id in to_prune:
        del stale_child_dbs[child_db_id]
    if to_prune:
        logger.info(
            "Notion KB sync: pruned %d aged-out stale child-database entr%s "
            "(no 404 recorded in the last %d days)",
            len(to_prune),
            "y" if len(to_prune) == 1 else "ies",
            _NOTION_CHILD_DB_PRUNE_DAYS,
        )
    return len(to_prune)


def _notion_blocks_to_markdown(blocks: list[dict]) -> str:
    """Convert Notion block objects to markdown text."""
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype) or {}

        # Extract rich text (reuse shared helper from notion provider)
        rich_texts = data.get("rich_text", []) or data.get("text", [])
        text = _rich_text_to_plain(rich_texts)

        if btype == "paragraph":
            lines.append(text)
        elif btype.startswith("heading_"):
            level = btype[-1]  # heading_1 → 1
            lines.append(f"{'#' * int(level)} {text}")
        elif btype == "bulleted_list_item":
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"1. {text}")
        elif btype == "to_do":
            checked = "x" if data.get("checked") else " "
            lines.append(f"- [{checked}] {text}")
        elif btype == "code":
            lang = data.get("language", "")
            lines.append(f"```{lang}\n{text}\n```")
        elif btype == "quote":
            lines.append(f"> {text}")
        elif btype == "divider":
            lines.append("---")
        elif btype == "toggle":
            lines.append(f"**{text}**")
        elif btype == "callout":
            emoji = (data.get("icon") or {}).get("emoji", "")
            lines.append(f"{emoji} {text}")
        elif btype == "child_page":
            child_title = data.get("title", "")
            if child_title:
                lines.append(f"[Sub-page: {child_title}]")
        elif btype == "child_database":
            child_title = data.get("title", "")
            if child_title:
                lines.append(f"[Database: {child_title}]")
        elif text:
            lines.append(text)

    return "\n\n".join(line for line in lines if line)


async def _fetch_all_blocks(
    client: httpx.AsyncClient,
    headers: dict,
    block_id: str,
    *,
    request_controller: _NotionRequestController | None = None,
    max_depth: int = _NOTION_MAX_BLOCK_DEPTH,
    _depth: int = 0,
) -> list[dict]:
    """Fetch all blocks under a parent, recursively including nested children.

    Handles pagination and recursion into content blocks (toggles, columns,
    synced blocks, etc.) to extract the full page content.  Skips recursion
    into ``child_page`` and ``child_database`` blocks — those represent
    separate pages and are followed by the main sync loop when
    ``recursive=True``.
    """
    if _depth >= max_depth:
        return []

    all_blocks: list[dict] = []
    start_cursor: str | None = None

    while True:
        url = f"{NOTION_API}/blocks/{block_id}/children?page_size=100"
        if start_cursor:
            url += f"&start_cursor={start_cursor}"
        if request_controller:
            resp = await request_controller.request(client, "GET", url, headers=headers)
        else:
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        blocks = data.get("results", [])

        for block in blocks:
            all_blocks.append(block)
            btype = block.get("type", "")
            # Recurse into content blocks (toggles, columns, etc.)
            # but NOT child_page/child_database (handled separately)
            if block.get("has_children") and btype not in (
                "child_page",
                "child_database",
            ):
                children = await _fetch_all_blocks(
                    client,
                    headers,
                    block["id"],
                    request_controller=request_controller,
                    max_depth=max_depth,
                    _depth=_depth + 1,
                )
                all_blocks.extend(children)

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return all_blocks


async def _resolve_notion_data_source_id(
    client: httpx.AsyncClient,
    headers: dict,
    database_id: str,
    *,
    request_controller: _NotionRequestController | None = None,
) -> str:
    """Resolve a database_id to its primary data_source_id (API 2025-09-03).

    Resolution strategy (max 2 HTTP calls):
    1. GET /databases/{id} → extract data_sources[0].id
    2. If that fails, GET /data_sources/{id} — the input may already be a
       data_source_id (e.g. after a workspace migration).

    Falls back to the original database_id if both steps fail.
    """
    # Step 1: fetch the database object and extract the primary data_source_id
    try:
        if request_controller:
            resp = await request_controller.request(
                client,
                "GET",
                f"{NOTION_API}/databases/{database_id}",
                headers=headers,
            )
        else:
            resp = await client.get(
                f"{NOTION_API}/databases/{database_id}",
                headers=headers,
            )
        if resp.status_code == 200:
            data = resp.json()
            data_sources = data.get("data_sources", [])
            if data_sources:
                ds_id = data_sources[0]["id"]
                logger.info(
                    "Notion KB sync: resolved database %s → data_source %s",
                    database_id,
                    ds_id,
                )
                return ds_id
    except Exception as exc:
        logger.debug(
            "Notion KB sync: failed to resolve data_source_id for %s (step 1): %s",
            database_id,
            format_exception_message(exc),
        )

    # Step 2: the input id might already be a valid data_source_id
    try:
        if request_controller:
            resp2 = await request_controller.request(
                client,
                "GET",
                f"{NOTION_API}/data_sources/{database_id}",
                headers=headers,
            )
        else:
            resp2 = await client.get(
                f"{NOTION_API}/data_sources/{database_id}",
                headers=headers,
            )
        if resp2.status_code == 200:
            logger.debug(
                "Notion KB sync: %s is already a valid data_source_id",
                database_id,
            )
            return database_id
    except Exception as exc:
        logger.debug(
            "Notion KB sync: failed to resolve data_source_id for %s (step 2): %s",
            database_id,
            format_exception_message(exc),
        )

    # DEBUG, not WARNING: the subsequent query typically fails right after and
    # the caller already logs "Failed to query child database ..." at WARNING —
    # double-warning would be redundant noise during recursive crawls.
    logger.debug(
        "Notion KB sync: could not resolve data_source_id for %s — will query with original id",
        database_id,
    )
    return database_id


async def _query_notion_database_pages(
    client: httpx.AsyncClient,
    headers: dict,
    database_id: str,
    *,
    request_controller: _NotionRequestController | None = None,
) -> list[str]:
    """Query a Notion database and return all page IDs (paginated).

    Uses the Notion API 2025-09-03 ``/v1/data_sources/{id}/query`` endpoint.
    Resolves ``database_id`` → ``data_source_id`` before the first query.  On
    a 404 response with a freshly-resolved data_source_id (stale cached id),
    re-resolves once and retries to handle workspaces whose data_source mapping
    has changed since the original resolution.  The retry only applies to the
    first page: pagination cursors are opaque and bound to the data source
    that issued them, so replaying one against a re-resolved id is invalid.
    """
    # Resolve database_id → data_source_id for API 2025-09-03
    ds_id = await _resolve_notion_data_source_id(
        client,
        headers,
        database_id,
        request_controller=request_controller,
    )

    async def _post_query(body: dict) -> httpx.Response:
        if request_controller:
            return await request_controller.request(
                client,
                "POST",
                f"{NOTION_API}/data_sources/{ds_id}/query",
                headers=headers,
                json=body,
            )
        return await client.post(
            f"{NOTION_API}/data_sources/{ds_id}/query",
            headers=headers,
            json=body,
        )

    page_ids: list[str] = []
    start_cursor: str | None = None
    stale_refreshed = False  # allow at most one fresh resolution per call

    while True:
        body: dict = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = await _post_query(body)

        # Stale data_source_id: re-resolve once and retry.  This handles the
        # case where the workspace's data_source mapping was updated after
        # the initial _resolve_notion_data_source_id call.  Only on the first
        # page — a start_cursor from the stale data source cannot be replayed
        # against a different one.
        if (
            resp.status_code == 404
            and ds_id != database_id
            and not stale_refreshed
            and start_cursor is None
        ):
            logger.info(
                "Notion KB sync: data_sources query returned 404 for %s"
                " (stale ds_id=%s), re-resolving and retrying",
                database_id,
                ds_id,
            )
            stale_refreshed = True
            ds_id = await _resolve_notion_data_source_id(
                client,
                headers,
                database_id,
                request_controller=request_controller,
            )
            resp = await _post_query(body)

        resp.raise_for_status()
        data = resp.json()
        page_ids.extend(
            r["id"] for r in data.get("results", []) if r.get("object") == "page"
        )
        logger.debug(
            "Notion KB sync: query page returned %d results (total=%d, has_more=%s)",
            len(data.get("results", [])),
            len(page_ids),
            data.get("has_more"),
        )
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return page_ids


def _extract_notion_id(raw: str) -> str:
    """Normalize a Notion page **or database** identifier.

    Notion shares page-ids and database-ids in the same URL / hex shape, so
    this helper is intentionally source-agnostic — it's wired into the
    ``page_id``, ``database_id`` and ``page_ids`` branches of
    ``sync_notion_pages``.

    Accepts:
      - Standard UUID with dashes (``xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx``)
      - 32-character hex string without dashes
      - Full Notion sharing URL
        (e.g. ``https://www.notion.so/<workspace>/<slug>-<uuid>?source=copy_link``)

    Returns the identifier in standard dashed UUID format.
    If the value cannot be recognised as a Notion identifier it is returned
    unchanged so the Notion API can produce a descriptive error.
    """
    raw = raw.strip()

    # Already a dashed UUID — return normalised to lowercase.
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    ):
        return raw.lower()

    # Strip query string and URL fragment so hex chars in ?source=… are ignored.
    path_part = raw.split("?")[0].split("#")[0]

    # Find all exactly-32-char hex runs in the path.  The last one is the
    # page UUID in a Notion slug (e.g. "My-Page-Title-<uuid>").
    matches = re.findall(
        r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])",
        path_part,
        re.IGNORECASE,
    )
    if matches:
        hex_id = matches[-1].lower()
        return f"{hex_id[:8]}-{hex_id[8:12]}-{hex_id[12:16]}-{hex_id[16:20]}-{hex_id[20:]}"

    # Unrecognised format — return as-is so the Notion API produces a clear error.
    return raw


def _load_stale_child_dbs(config: dict) -> dict:
    """Build the mutable stale child-database tracking dict from a source config.

    Reads ``config["stale_child_dbs"]`` and returns a fresh copy (so in-place
    mutation during the sync never aliases the persisted/ORM value).  Corrupt
    values are ignored defensively: a non-dict top-level value (hand-edited
    config) yields an empty dict, and non-dict entries are dropped.

    Legacy one-shot ``skipped_child_db_ids`` entries (the list-based mechanism
    this counted dict supersedes) are migrated to counted entries already at
    the stale threshold, so previously skipped child databases stay skipped
    and enter the weekly re-check / recovery cycle.  The legacy key itself is
    removed from the persisted config by :func:`_persist_stale_child_dbs`.
    """
    raw = config.get("stale_child_dbs")
    stale: dict = (
        {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}
        if isinstance(raw, dict)
        else {}
    )
    legacy = config.get("skipped_child_db_ids")
    if isinstance(legacy, (list, tuple)):
        now_str = datetime.now(UTC).isoformat()
        for child_db_id in legacy:
            if isinstance(child_db_id, str) and child_db_id not in stale:
                stale[child_db_id] = {
                    "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
                    "first_failed_at": now_str,
                    "last_failed_at": now_str,
                }
    return stale


async def _persist_stale_child_dbs(
    db: AsyncSession,
    source_id: str,
    stale_child_dbs: dict,
) -> None:
    """Persist the per-child-DB 404 tracking state to the sync source config.

    Writes ``config["stale_child_dbs"]`` (or removes the key when the dict is
    empty) and drops the legacy one-shot ``skipped_child_db_ids`` list it
    supersedes, so the two mechanisms cannot diverge.  A no-op when the
    resulting config equals what is already persisted.

    Best-effort: a failure here only means the next sync re-encounters the
    same 404s again — the main sync result is never affected.

    Recovery paths: the weekly re-check window retries stale entries
    automatically, and the force-resync endpoint clears the whole tracking
    state so a re-shared / restored child database is picked up immediately.
    """
    try:
        source = await db.get(KBSyncSource, uuid.UUID(str(source_id)))
        if source is None:
            return
        config = dict(source.config or {})
        new_config = dict(config)
        if stale_child_dbs:
            new_config["stale_child_dbs"] = stale_child_dbs
        else:
            new_config.pop("stale_child_dbs", None)
        new_config.pop("skipped_child_db_ids", None)
        if new_config == config:
            return  # nothing changed — avoid a needless row update
        # Plain JSON column — reassign a fresh dict so the ORM change tracker
        # picks up the mutation (same pattern as _persist_resolved_default_branch).
        source.config = new_config
        await db.flush()
    except Exception as exc:
        logger.warning(
            "Could not persist stale child-database state for sync source %s: %s",
            source_id,
            format_exception_message(exc),
        )


def _count_stale_child_db_failure(
    stale_child_databases: dict[str, int] | None,
    counted_this_run: set[str],
    child_db_id: str,
    kb_name: str,
) -> None:
    """Increment the non-404 failure counter for a child database.

    Increments at most once per sync run (``counted_this_run`` guard) so the
    counter keeps its "consecutive sync cycles" semantics even when the same
    child database is referenced as a block from multiple pages.  Emits an
    elevated warning exactly when the count first reaches
    ``_NOTION_STALE_CHILD_DB_THRESHOLD`` (the quarantine trigger).  No-op when
    tracking is disabled (``stale_child_databases is None``).
    """
    if stale_child_databases is None or child_db_id in counted_this_run:
        return
    counted_this_run.add(child_db_id)
    new_count = stale_child_databases.get(child_db_id, 0) + 1
    stale_child_databases[child_db_id] = new_count
    if new_count == _NOTION_STALE_CHILD_DB_THRESHOLD:
        logger.warning(
            "Notion KB sync: child database %s in KB '%s' has failed %d "
            "consecutive sync cycle(s) and will be skipped on future syncs. "
            "Check that the Notion database still exists and that the "
            "integration has access to it; force a re-sync to reset.",
            child_db_id,
            kb_name,
            new_count,
        )


async def sync_notion_pages(
    db: AsyncSession,
    kb: KnowledgeBase,
    user_id: uuid.UUID | str,
    page_ids: list[str] | None = None,
    search_query: str | None = None,
    page_id: str | None = None,
    database_id: str | None = None,
    recursive: bool = False,
    integration_id: str | None = None,
    sync_source_id: str | None = None,
    stale_child_dbs: dict | None = None,
    stale_child_databases: dict[str, int] | None = None,
    skip_page_ids: frozenset[str] | None = None,
    _not_found_pages_out: list[str] | None = None,
    _error_collector: "list[tuple[str, str]] | None" = None,
) -> list[str]:
    """Sync Notion pages into a knowledge base (full-refresh).

    Supports multiple sync modes (evaluated in priority order):

      1. **page_id** — sync a single page (optionally recursive).
      2. **database_id** — sync all pages from a Notion database.
      3. **page_ids** — sync an explicit list of page IDs.
      4. **search_query / all** — search Notion and sync matching pages.

    When ``recursive=True``, child pages and child databases discovered
    within synced pages are automatically followed and indexed (BFS).

    ``sync_source_id``: the :class:`KBSyncSource` id behind this sync: the
    owner of the documents it imports, whose stale sweep leaves the other
    Notion sources' documents alone, and where the updated
    ``stale_child_dbs`` tracking state is persisted
    (:func:`_persist_stale_child_dbs`).  The direct API endpoint omits it:
    its import owns only the unowned Notion documents.

    ``stale_child_dbs`` is an optional mutable dict that tracks consecutive
    404 failures for each child-database block discovered during a recursive
    sync (key = child-database block ID, value = ``{"count": int,
    "first_failed_at": str, "last_failed_at": str}``).  When provided it is
    updated in-place, and persisted back to the sync-source config when
    ``sync_source_id`` is also given — suppressing repeated 404 log noise for
    permanently-missing Notion child databases.  Entries whose last failure
    is older than ``_NOTION_CHILD_DB_PRUNE_DAYS`` are pruned at the start of
    the sync so the persisted dict cannot grow without bound.  Build the dict
    from a persisted config with :func:`_load_stale_child_dbs` (which also
    migrates the legacy one-shot ``skipped_child_db_ids`` list).

    ``stale_child_databases``: optional mutable dict (``{db_id: count}``)
    tracking how many consecutive sync cycles each child database has failed
    with a NON-404 error (404s take the counted ``stale_child_dbs`` path
    above).  A failure increments the count at most once per run; when
    the count reaches ``_NOTION_STALE_CHILD_DB_THRESHOLD`` the database is
    quarantined: skipped on this and all future syncs without being
    re-queried.  Below the threshold, a successful query removes the entry
    (transient failures self-heal).  A quarantined database is never probed
    again, so recovery requires clearing its entry externally — the force
    re-sync endpoint does this.  The caller is responsible for persisting
    the updated dict back to the sync source config.

    ``skip_page_ids`` is an optional frozenset of page IDs that have been
    quarantined after repeated 404/410 responses (populated by the scheduler
    from ``source.config["skipped_stale_page_ids"]``).  These pages are
    skipped without any Notion API call — a single info-level log line
    replaces the repeated per-cycle 404 warning.

    ``_not_found_pages_out``, when provided, is populated with every page ID
    that returns a permanent HTTP 404 or 410 response during this sync run.
    The scheduler uses these to increment per-page failure counters and
    quarantine pages that fail consistently.

    ``_error_collector``, when provided, receives ``(page_id, error_message)``
    tuples for every page that failed — the scheduler uses this to track
    partial sync failures across consecutive runs.

    Returns list of all synced document IDs.

    Raises on total failure — a run where every queued page
    failed never returns ``[]`` as a spurious success:

      - explicit page modes (``page_id`` / ``page_ids``) re-raise the first
        page error, preferring a permanent one (400/404/410 on
        ``/v1/pages/{id}``) so the scheduler's permanent-failure classifier
        sees it;
      - non-explicit modes (search) raise
        :class:`NotionSyncTotalFailureError` aggregating the per-page errors.
    """
    from collections import deque

    max_pages = _MAX_NOTION_PAGES_SYNC

    # Drop aged-out tracking entries (child databases that vanished from the
    # workspace) so the persisted dict cannot grow without bound.
    if stale_child_dbs is not None:
        _prune_stale_child_db_entries(stale_child_dbs)

    token = await _get_integration_token(
        db, user_id, "notion", integration_id=integration_id
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    all_doc_ids: set[str] = set()  # All docs seen in this sync
    embed_doc_ids: list[str] = []  # Docs that need (re-)embedding
    page_errors: list[tuple[str, str]] = []  # (page_id, error_msg) for failed pages
    # Child DBs that already returned 404 during THIS run: re-encounters are
    # skipped without a new API call (the same stale child database can be
    # referenced as a block from multiple pages), and — just as importantly —
    # each child DB counts at most ONE 404 per run so the "consecutive sync
    # runs" threshold cannot be crossed within a single sync cycle.
    run_404_child_db_ids: set[str] = set()
    # Child DBs whose non-404 failure counter was already incremented during
    # THIS run — the same child database can be re-encountered from multiple
    # pages, and the counter must grow by at most 1 per sync cycle to keep
    # its "consecutive sync cycles" semantics.
    stale_counted_this_run: set[str] = set()
    # Quarantined child DBs whose recovery probe already FIRED this run —
    # distinct from ``stale_counted_this_run`` (the non-404 increment guard
    # used by ``_count_stale_child_db_failure``): a probe that SUCCEEDS
    # clears the tracking entry without touching either counter guard, so
    # without this set a re-encounter would recompute the recheck cadence
    # against mutated state. Both quarantine outcomes (skip and probe) are
    # frozen at the first encounter of a run.
    probed_quarantined_this_run: set[str] = set()

    async with egress_client(timeout=30) as client:
        request_controller = _NotionRequestController()
        explicit_requested_page_ids: set[str] = set()
        first_explicit_page_error: Exception | None = None
        first_permanent_page_error: Exception | None = None

        # ── Seed the page queue ────────────────────────────────────────
        page_queue: deque[str] = deque()
        processed_page_ids: set[str] = set()

        if page_id:
            # Mode 1: specific page
            normalized_page_id = _extract_notion_id(page_id)
            page_queue.append(normalized_page_id)
            explicit_requested_page_ids.add(normalized_page_id)
        elif database_id:
            # Mode 2: all pages from a Notion database
            db_pages = await _query_notion_database_pages(
                client,
                headers,
                _extract_notion_id(database_id),
                request_controller=request_controller,
            )
            page_queue.extend(db_pages)
            logger.info(
                "Notion sync: database %s contains %d page(s)",
                database_id,
                len(db_pages),
            )
        elif page_ids:
            # Mode 3: explicit list of page IDs (backward compat)
            normalized_page_ids = [_extract_notion_id(pid) for pid in page_ids]
            page_queue.extend(normalized_page_ids)
            explicit_requested_page_ids.update(normalized_page_ids)
        else:
            # Mode 4: search for pages (paginated)
            start_cursor: str | None = None
            while True:
                body: dict = {
                    "filter": {"value": "page", "property": "object"},
                    "page_size": 100,
                }
                if search_query:
                    body["query"] = search_query
                if start_cursor:
                    body["start_cursor"] = start_cursor
                resp = await request_controller.request(
                    client,
                    "POST",
                    f"{NOTION_API}/search",
                    headers=headers,
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
                page_queue.extend(r["id"] for r in data.get("results", []))
                if not data.get("has_more"):
                    break
                start_cursor = data.get("next_cursor")

        # ── Process page queue (BFS) ───────────────────────────────────
        while page_queue and len(processed_page_ids) < max_pages:
            current_page_id = page_queue.popleft()
            if current_page_id in processed_page_ids:
                continue
            processed_page_ids.add(current_page_id)

            # Skip pages quarantined after repeated 404/410 responses.
            # The scheduler populates skip_page_ids from the source config
            # (``skipped_stale_page_ids``) so stale references no longer
            # pollute the log on every sync cycle.
            if skip_page_ids and current_page_id in skip_page_ids:
                logger.info(
                    "Notion sync: skipping quarantined page %s for KB '%s' "
                    "(flagged after repeated 404/410 responses — remove the "
                    "reference from the sync source or use Force Re-sync to "
                    "reset the counter)",
                    current_page_id,
                    kb.name,
                )
                continue

            try:
                # Fetch page metadata
                page_resp = await request_controller.request(
                    client,
                    "GET",
                    f"{NOTION_API}/pages/{current_page_id}",
                    headers=headers,
                )
                page_resp.raise_for_status()
                page_data = page_resp.json()

                # Extract title
                title = "Untitled"
                props = page_data.get("properties", {})
                for prop in props.values():
                    if prop.get("type") == "title":
                        title_parts = prop.get("title", [])
                        if title_parts:
                            title = _rich_text_to_plain(title_parts)
                        break

                # Fetch blocks — recursively includes nested content
                # (toggles, columns, synced blocks, etc.)
                blocks = await _fetch_all_blocks(
                    client,
                    headers,
                    current_page_id,
                    request_controller=request_controller,
                )

                # If recursive mode, discover child pages and databases
                if recursive:
                    for block in blocks:
                        btype = block.get("type", "")
                        if btype == "child_page":
                            child_id = block["id"]
                            if child_id not in processed_page_ids:
                                page_queue.append(child_id)
                        elif btype == "child_database":
                            child_db_id = block["id"]

                            # Re-encounter of a child DB that already 404'd
                            # during THIS run (the same stale child database
                            # can be referenced as a block from multiple
                            # pages): skip without another guaranteed-fail
                            # API call, and without double-counting — the
                            # stale threshold counts consecutive sync RUNS,
                            # so each run must record at most one 404.
                            if child_db_id in run_404_child_db_ids:
                                logger.debug(
                                    "Notion KB sync: skipping child database %s "
                                    "(already returned 404 earlier in this run)",
                                    child_db_id,
                                )
                                continue

                            # Re-encounter of a quarantined child DB whose
                            # recovery probe already fired this run: never
                            # probe twice in one run.  Checked before the
                            # stale-tracking branches because a SUCCESSFUL
                            # probe clears the tracking entry (the DB would
                            # otherwise look healthy and be queried again).
                            if child_db_id in probed_quarantined_this_run:
                                logger.debug(
                                    "Notion KB sync: skipping child database %s "
                                    "(recovery probe already ran earlier in "
                                    "this run)",
                                    child_db_id,
                                )
                                continue

                            # Skip child databases that have returned 404 enough
                            # consecutive times to be marked stale — emit a single
                            # DEBUG line instead of a WARNING every sync cycle.
                            if stale_child_dbs is not None and _is_notion_child_db_stale(
                                stale_child_dbs, child_db_id
                            ):
                                stale_entry = stale_child_dbs.get(child_db_id, {})
                                logger.debug(
                                    "Notion KB sync: skipping known-stale child database %s "
                                    "(%d consecutive 404s, last seen: %s)",
                                    child_db_id,
                                    stale_entry.get("count", 0),
                                    stale_entry.get("last_failed_at", "unknown"),
                                )
                                continue

                            # Skip child databases quarantined after repeated
                            # NON-404 failures (e.g. persistent 400s from stale
                            # data_source references).  info, not
                            # warning: the elevated warning fired once at the
                            # threshold crossing; repeating it every cycle
                            # would recreate the log noise the quarantine
                            # removes.  Every Nth quarantined run the database
                            # is probed again instead of skipped so a
                            # repaired reference recovers without a manual
                            # force re-sync; the counter is bumped on skipped
                            # runs (once per run) purely to advance that
                            # modulo cadence.
                            if stale_child_databases is not None:
                                fail_count = stale_child_databases.get(
                                    child_db_id, 0
                                )
                                if fail_count >= _NOTION_STALE_CHILD_DB_THRESHOLD:
                                    # Re-encounter within the SAME run (the same
                                    # quarantined child DB can be referenced as
                                    # a block from multiple pages): the counter
                                    # was already advanced and the skip already
                                    # logged, and the recheck decision below
                                    # must not be recomputed against the
                                    # just-mutated count (it could flip
                                    # mid-run) — skip silently.
                                    if child_db_id in stale_counted_this_run:
                                        logger.debug(
                                            "Notion KB sync: skipping quarantined "
                                            "child database %s (already handled "
                                            "earlier in this run)",
                                            child_db_id,
                                        )
                                        continue
                                    skips_since_quarantine = (
                                        fail_count - _NOTION_STALE_CHILD_DB_THRESHOLD
                                    )
                                    due_for_recheck = (
                                        skips_since_quarantine
                                        % _NOTION_STALE_CHILD_DB_RECHECK_EVERY
                                        == _NOTION_STALE_CHILD_DB_RECHECK_EVERY - 1
                                    )
                                    if not due_for_recheck:
                                        stale_counted_this_run.add(child_db_id)
                                        new_count = fail_count + 1
                                        stale_child_databases[child_db_id] = (
                                            new_count
                                        )
                                        # "failing/skipped", not "failed": once
                                        # quarantined the counter also advances
                                        # on skipped runs (cadence ticks), so it
                                        # is no longer a pure failure count.
                                        # Log the POST-increment value so the
                                        # count includes the current skipped
                                        # cycle.
                                        logger.info(
                                            "Notion KB sync: skipping quarantined "
                                            "child database %s for KB '%s' — %d "
                                            "failing/skipped sync cycle(s) since "
                                            "the first failure, including this "
                                            "one (quarantine threshold=%d). It "
                                            "will be probed again automatically; "
                                            "force a re-sync to retry "
                                            "immediately.",
                                            child_db_id,
                                            kb.name,
                                            new_count,
                                            _NOTION_STALE_CHILD_DB_THRESHOLD,
                                        )
                                        continue
                                    # Freeze the probe decision for this run —
                                    # a re-encounter must not probe again even
                                    # when this probe succeeds and clears the
                                    # tracking entry (see the guard above).
                                    probed_quarantined_this_run.add(child_db_id)
                                    # "after N cycles", not "N so far": the
                                    # probe's own outcome is not known yet
                                    # (success clears the entry, a failure
                                    # increments the counter), so the message
                                    # only counts completed cycles.
                                    logger.info(
                                        "Notion KB sync: recovery probe for "
                                        "quarantined child database %s in KB '%s' "
                                        "after %d failing/skipped cycle(s)",
                                        child_db_id,
                                        kb.name,
                                        fail_count,
                                    )

                            try:
                                child_pages = await _query_notion_database_pages(
                                    client,
                                    headers,
                                    child_db_id,
                                    request_controller=request_controller,
                                )
                                for p in child_pages:
                                    if p not in processed_page_ids:
                                        page_queue.append(p)
                                # Successful query — clear any prior stale state so
                                # a recovered database is retried on the next run too.
                                if stale_child_dbs is not None and child_db_id in stale_child_dbs:
                                    prior_count = stale_child_dbs[child_db_id].get("count", 0)
                                    del stale_child_dbs[child_db_id]
                                    logger.info(
                                        "Notion KB sync: child database %s recovered "
                                        "(previously stale with %d consecutive 404s)",
                                        child_db_id,
                                        prior_count,
                                    )
                                # Also clear any prior NON-404 failure count so
                                # a recovered child database is no longer
                                # penalised.
                                if stale_child_databases is not None:
                                    stale_child_databases.pop(child_db_id, None)
                            except Exception as e:
                                error_msg = format_exception_message(e)
                                # Structural check, not string matching: the query
                                # helper surfaces Notion API errors via
                                # ``raise_for_status()`` as httpx.HTTPStatusError.
                                is_404 = (
                                    isinstance(e, httpx.HTTPStatusError)
                                    and e.response.status_code == 404
                                )
                                if is_404:
                                    # Re-encounters later in this run skip the
                                    # API call and the counter (one 404 per
                                    # child DB per run).
                                    run_404_child_db_ids.add(child_db_id)

                                if stale_child_dbs is not None and is_404:
                                    # First 404 after a non-404 failure streak
                                    # (e.g. HTTP 400s before the Notion API
                                    # endpoint migration): carry the prior
                                    # count over so the stale threshold is not
                                    # reset by the error-type change.
                                    _seed_404_counter_from_non404_failures(
                                        stale_child_dbs,
                                        stale_child_databases,
                                        child_db_id,
                                    )
                                    count = _record_child_db_404(stale_child_dbs, child_db_id)
                                    recheck_date = (
                                        datetime.now(UTC)
                                        + timedelta(days=_NOTION_CHILD_DB_RECHECK_DAYS)
                                    ).strftime("%Y-%m-%d")
                                    if count == _NOTION_CHILD_DB_STALE_THRESHOLD:
                                        # One-time warning when first reaching the stale threshold
                                        logger.warning(
                                            "Notion KB sync: child database %s has returned 404 "
                                            "for %d consecutive sync runs — marking as stale and "
                                            "suppressing further warnings until %s. "
                                            "Verify this Notion database still exists or "
                                            "update the KB sync source.",
                                            child_db_id,
                                            count,
                                            recheck_date,
                                        )
                                    elif count > _NOTION_CHILD_DB_STALE_THRESHOLD:
                                        # Re-check attempt (weekly window expired) — still failing
                                        logger.warning(
                                            "Notion KB sync: child database %s is still stale "
                                            "after re-check (%d consecutive 404s) — "
                                            "skipping until %s.",
                                            child_db_id,
                                            count,
                                            recheck_date,
                                        )
                                    else:
                                        # Below threshold — emit the normal per-run warning
                                        logger.warning(
                                            "Failed to query child database %s: %s",
                                            child_db_id,
                                            error_msg,
                                        )
                                else:
                                    # Non-404 error (or 404 with tracking
                                    # disabled): emit the normal warning.  A
                                    # non-404 failure resets the 404 counter
                                    # so only *consecutive* 404s trigger the
                                    # stale guard, and it increments the
                                    # NON-404 counter at most once
                                    # per run.
                                    if stale_child_dbs is not None and not is_404:
                                        stale_child_dbs.pop(child_db_id, None)
                                    logger.warning(
                                        "Failed to query child database %s: %s",
                                        child_db_id,
                                        error_msg,
                                    )
                                    if not is_404:
                                        _count_stale_child_db_failure(
                                            stale_child_databases,
                                            stale_counted_this_run,
                                            child_db_id,
                                            kb.name,
                                        )

                content = f"# {title}\n\n{_notion_blocks_to_markdown(blocks)}"
                page_url = page_data.get(
                    "url",
                    f"https://notion.so/{current_page_id.replace('-', '')}",
                )

                async with db.begin_nested():
                    doc, needs_embed = await _upsert_document(
                        db,
                        kb,
                        f"{title}.md",
                        content,
                        "notion",
                        source_url=page_url,
                        sync_source_id=sync_source_id,
                    )
                # Record the doc only AFTER the savepoint released
                # successfully — if the release itself fails (late flush
                # error, disconnect), control jumps to the per-page except
                # below, and recording the id of a rolled-back upsert would
                # make _delete_stale_documents treat live documents as stale.
                if doc:
                    all_doc_ids.add(str(doc.id))
                    if needs_embed:
                        embed_doc_ids.append(str(doc.id))

            except Exception as e:
                # Collect pages that return a permanent "not found" status
                # (HTTP 404 / 410) so the scheduler can track consecutive
                # failures and quarantine stale references.
                # Only when the failing request targeted THIS page's own id
                # (GET /v1/pages/{id} or the top-level
                # /v1/blocks/{id}/children fetch): a 404 from a NESTED
                # block-children request (deleted/inaccessible child block)
                # says nothing about the page itself and must not push a
                # live page toward quarantine.
                if (
                    _not_found_pages_out is not None
                    and isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code in (404, 410)
                    and current_page_id in str(e.request.url)
                ):
                    _not_found_pages_out.append(current_page_id)
                err_msg = format_exception_message(e)
                logger.warning(
                    "Failed to sync Notion page %s: %s", current_page_id, err_msg
                )
                page_errors.append((current_page_id, err_msg))
                if _error_collector is not None:
                    _error_collector.append((current_page_id, err_msg))
                if current_page_id in explicit_requested_page_ids:
                    if first_explicit_page_error is None:
                        first_explicit_page_error = e
                    if (
                        first_permanent_page_error is None
                        and _is_notion_permanent_page_sync_error(e, page_id=current_page_id)
                    ):
                        first_permanent_page_error = e

        # Explicit page modes (page_id / page_ids) with NOTHING synced must
        # fail hard instead of falling through to the stale-delete + commit
        # path below — that path would wipe every previously synced Notion
        # document from the KB and record the run as a success. Prefer the
        # permanent page error (stale/deleted reference — 400/404/410 on
        # /v1/pages/{id}) so the scheduler's permanent-failure classifier
        # sees it and the perm-fail quarantine budget accumulates; otherwise
        # re-raise the first transient failure so the scheduler retries with
        # backoff and existing documents survive the outage.
        if (
            explicit_requested_page_ids
            and not all_doc_ids
            and first_explicit_page_error is not None
        ):
            raise first_permanent_page_error or first_explicit_page_error

    # Skip the stale-document sweep only on a TOTAL FAILURE (page-level
    # errors and nothing synced) — that run is about to raise, and deleting
    # against an empty id set would wipe existing KB content while the
    # remote pages are temporarily (or permanently) inaccessible.
    # A clean run that legitimately yields zero pages (empty
    # database, no search results) still sweeps, preserving the
    # full-refresh contract.
    if page_errors and not all_doc_ids:
        deleted = 0
    else:
        deleted = await _delete_stale_documents(
            db, kb, "notion", all_doc_ids, sync_source_id=sync_source_id
        )
    await _recount_kb_stats(db, kb, content_changed=bool(embed_doc_ids or deleted))
    # Persist the updated per-child-DB 404 tracking state so the stale-skip
    # logic (and the one-time threshold warning) survive across sync cycles.
    # Only done when a sync_source_id is provided (scheduled syncs); the
    # direct API endpoint omits it because it doesn't use recursive mode.
    # No-op when nothing changed — the helper compares against the persisted
    # config before writing.
    if sync_source_id and stale_child_dbs is not None:
        await _persist_stale_child_dbs(db, sync_source_id, stale_child_dbs)
    await db.commit()
    await enqueue_documents(embed_doc_ids)

    if page_errors and not all_doc_ids:
        # Total failure: every page in the queue failed.  Raise so the
        # scheduler's failure-count and quarantine logic can detect
        # permanently broken page references and eventually disable the
        # source — instead of recording an indefinite chain of spurious
        # "success" runs with 0 documents.  Raised before the
        # summary log below so a doomed run never emits a misleading
        # "synced" info line.
        error_detail = "; ".join(f"{pid}: {msg[:80]}" for pid, msg in page_errors[:3])
        if len(page_errors) > 3:
            error_detail += f" (and {len(page_errors) - 3} more)"
        raise NotionSyncTotalFailureError(
            f"All {len(page_errors)} Notion page(s) failed to sync: {error_detail}"
        )

    if deleted:
        logger.info(
            "Notion sync: removed %d stale document(s) from KB '%s'", deleted, kb.name
        )
    logger.info(
        "Notion sync for KB '%s': %d page(s) synced, %d new/updated, %d removed",
        kb.name,
        len(all_doc_ids),
        len(embed_doc_ids),
        deleted,
    )

    if page_errors:
        # Partial failure: some pages succeeded, some didn't.  Log a
        # structured warning so operators can identify stale references
        # even when the overall sync is healthy.
        failed_ids_preview = ", ".join(pid for pid, _ in page_errors[:5])
        if len(page_errors) > 5:
            failed_ids_preview += f" (and {len(page_errors) - 5} more)"
        logger.warning(
            "Notion sync for KB '%s': %d page(s) failed to sync "
            "(stale or inaccessible references) — %s",
            kb.name,
            len(page_errors),
            failed_ids_preview,
        )

    return list(all_doc_ids)


# ---------------------------------------------------------------------------
# Microsoft OneDrive / SharePoint sync
# ---------------------------------------------------------------------------


async def _resolve_sharepoint_site_id(
    client: httpx.AsyncClient,
    headers: dict,
    site_id: str,
) -> str:
    """Resolve a SharePoint site identifier to a full Graph API site ID.

    Accepts any of:
      - Full composite ID ``hostname,collection-guid,site-guid`` → used as-is.
      - Full SharePoint URL (e.g.
        ``https://tenant.sharepoint.com/sites/my-site``) → parsed into
        hostname + relative path and resolved via the deterministic
        ``/sites/{hostname}:/{path}`` Graph endpoint.
      - Site name or slug (e.g. ``general-administration-legal``) → first
        attempted via the deterministic path endpoint (assumes the default
        ``/sites/{slug}`` path on the tenant root), then falls back to the
        ``/sites?search=`` endpoint, preferring an exact name match.

    Raises :class:`KBSyncConfigError` (before any network call) when
    *site_id* is a SharePoint sharing link (``/:<type>:/s/…`` format, e.g.
    ``/:f:``, ``/:u:``, ``/:b:``) — a permanent misconfiguration the
    scheduler uses to disable the source.
    """
    from urllib.parse import urlparse

    # Full composite ID (contains commas) — use directly
    if "," in site_id:
        return site_id

    # ── Reject sharing URLs early (permanent config error) ────────────
    # SharePoint personal sharing links (/:f:/s/..., /:u:/s/..., /:b:/s/...
    # and any other /:<type>:/ variant) look like URLs but are not valid
    # site identifiers — resolving them via the Graph
    # search API always fails. Detect the pattern here to fail fast without
    # making any network calls, and raise KBSyncConfigError so the scheduler
    # can disable the source and notify the owner.
    if _is_sharepoint_sharing_url(site_id):
        # Never echo the sharing link itself: the encoded path segment and
        # the ?e= query parameter carry the share token, and this message is
        # persisted to last_sync_error, logged, and broadcast via SSE — only
        # the hostname is safe to surface.
        redacted_host = urlparse(site_id).hostname or "<sharepoint host>"
        raise KBSyncConfigError(
            f"The configured site_id appears to be a SharePoint sharing link "
            f"(https://{redacted_host}/:…:/s/…, link redacted), not a "
            "canonical site URL. SharePoint sharing links (the /:f:/s/…, "
            "/:u:/s/…, /:b:/s/… and similar /:<type>:/ formats) cannot be "
            "used as site identifiers. Provide the full SharePoint site URL "
            "(e.g. https://tenant.sharepoint.com/sites/my-site) or the full "
            "site ID (hostname,collection-id,site-id) instead."
        )

    # ── Parse full SharePoint URL ──────────────────────────────────────
    # e.g. https://tenant.sharepoint.com/sites/my-site
    hostname: str | None = None
    site_path: str | None = None

    if re.match(r"https?://", site_id, re.IGNORECASE):
        parsed = urlparse(site_id)
        hostname = parsed.hostname  # e.g. tenant.sharepoint.com
        # Strip leading "/" so we get "sites/my-site"
        site_path = parsed.path.strip("/") or None

    # ── Deterministic path-based resolution ────────────────────────────
    # Works for full URLs.  Plain slugs skip this (no hostname available)
    # and fall through to the search-based resolution below.
    path_url = (
        f"{GRAPH_API}/sites/{hostname}:/{site_path}" if hostname and site_path else None
    )

    if path_url:
        path_resp = await client.get(path_url, headers=headers)
        if path_resp.status_code == 200:
            data = path_resp.json()
            resolved = data.get("id", "")
            if resolved:
                logger.info(
                    "Resolved SharePoint site '%s' via path → id '%s'",
                    site_id,
                    resolved,
                )
                return resolved

    # ── Fallback: search-based resolution ──────────────────────────────
    # The search index is async and may miss recently-created sites, but
    # it works when we don't have the full URL / hostname.
    search_term = site_path.rsplit("/", 1)[-1] if site_path else site_id
    search_resp = await client.get(
        f"{GRAPH_API}/sites",
        headers=headers,
        params={"search": search_term},
    )
    search_resp.raise_for_status()
    sites = search_resp.json().get("value", [])

    if not sites:
        raise ValueError(
            f"SharePoint site '{site_id}' not found. "
            f"Provide the full SharePoint URL "
            f"(e.g. https://tenant.sharepoint.com/sites/my-site) "
            f"or the full site ID (hostname,collection-id,site-id)."
        )

    # Prefer exact name match, fall back to first result
    slug = search_term.strip().lower()
    for site in sites:
        if site.get("name", "").lower() == slug:
            logger.info(
                "Resolved SharePoint site name '%s' → id '%s'",
                site_id,
                site["id"],
            )
            return site["id"]

    resolved = sites[0]["id"]
    logger.info(
        "Resolved SharePoint site name '%s' → id '%s' (first search result)",
        site_id,
        resolved,
    )
    return resolved


def _normalize_for_comparison(s: str) -> str:
    """Strip accents and lowercase for fuzzy name matching.

    SharePoint drive display names may contain accents (e.g. "Documents
    partagés") while URL paths use the unaccented form ("Documents
    partages").  This helper enables matching across both forms.
    """
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _is_sharepoint_sharing_url(s: str) -> bool:
    """Return True if *s* looks like a SharePoint sharing URL.

    SharePoint sharing links have the form::

        https://<tenant>.sharepoint.com/:<type>:/s/<site>/<encoded-id>?e=<token>

    where ``<type>`` is a single letter (``f`` for folder, ``u`` for file,
    ``b`` for blob, etc.).  These URLs are *not* valid folder paths and must
    be resolved via the Graph API ``/shares`` endpoint before use.
    """
    return bool(re.match(r"https?://[^/]+\.sharepoint\.com/:", s, re.IGNORECASE))


async def _resolve_drive_sharing_url(
    client: httpx.AsyncClient,
    headers: dict,
    sharing_url: str,
) -> tuple[str, str]:
    """Resolve a SharePoint sharing URL to a ``(drive_base, children_url)`` pair.

    SharePoint sharing URLs (e.g.
    ``https://tenant.sharepoint.com/:f:/s/site/AABB?e=token``) cannot be
    used as folder paths in the Graph API.  This helper encodes the URL into
    a share ID using the ``u!<base64url>`` scheme and calls the
    ``/shares/{shareId}/driveItem`` endpoint to look up the target item.

    Returns ``(drive_base, start_url)`` where *drive_base* is
    ``https://graph.microsoft.com/v1.0/drives/{driveId}`` and *start_url*
    is the ``/children`` URL for the resolved folder item.

    Raises :class:`ValueError` on lookup failure or if the response does not
    contain the expected ``driveId`` / ``id`` fields.
    """
    import base64

    encoded = base64.urlsafe_b64encode(sharing_url.encode()).rstrip(b"=").decode()
    share_id = f"u!{encoded}"

    resp = await client.get(
        f"{GRAPH_API}/shares/{share_id}/driveItem",
        headers=headers,
    )
    if resp.status_code != 200:
        raise ValueError(
            f"Failed to resolve SharePoint sharing URL (HTTP {resp.status_code}). "
            "Ensure the link is a valid sharing URL and the integration has "
            "access to the shared folder. Alternatively, provide a folder path "
            "(e.g. /Documents/MyFolder) instead of a sharing link."
        )

    item = resp.json()
    drive_id = item.get("parentReference", {}).get("driveId")
    item_id = item.get("id")

    if not drive_id or not item_id:
        raise ValueError(
            "Could not extract driveId/itemId from the sharing URL response. "
            "Please provide a folder path (e.g. /Documents/MyFolder) instead "
            "of a sharing link."
        )

    base = f"{GRAPH_API}/drives/{drive_id}"
    start_url = f"{base}/items/{item_id}/children"
    logger.info(
        "Resolved SharePoint sharing URL → driveId='%s' itemId='%s'",
        drive_id,
        item_id,
    )
    return base, start_url


async def _resolve_sharepoint_drive(
    client: httpx.AsyncClient,
    headers: dict,
    site_id: str,
    folder_path: str,
) -> tuple[str, str]:
    """Resolve a SharePoint folder path whose first segment may be a document library name.

    SharePoint sites can have multiple document libraries (drives).  Users
    often copy paths like ``/Documents partages/Sub/Folder`` where
    ``Documents partages`` is the *display name* of a library, not a folder
    inside the default library.

    This helper lists all drives on the site and checks whether the first
    path segment matches a library name.  Matching is accent-insensitive
    (e.g. "Documents partages" matches "Documents partagés") and also checks
    the last segment of each drive's ``webUrl`` as a fallback.  If a match is
    found, the matching drive is used and the first segment is stripped from
    the path.

    Returns ``(drive_base_url, remaining_folder_path)``.
    """
    from urllib.parse import unquote

    site_base = f"{GRAPH_API}/sites/{site_id}"
    path = folder_path.strip("/")

    if not path:
        return f"{site_base}/drive", "/"

    parts = path.split("/", 1)
    first_segment = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    first_norm = _normalize_for_comparison(first_segment)

    try:
        resp = await client.get(f"{site_base}/drives", headers=headers)
        if resp.status_code == 200:
            drives = resp.json().get("value", [])
            for drive in drives:
                drive_name = drive.get("name", "")
                drive_id = drive["id"]

                # Match by display name (accent-insensitive)
                if _normalize_for_comparison(drive_name) == first_norm:
                    logger.info(
                        "Resolved SharePoint document library '%s' (name='%s') → drive id '%s'",
                        first_segment,
                        drive_name,
                        drive_id,
                    )
                    return f"{site_base}/drives/{drive_id}", f"/{rest}" if rest else "/"

                # Match by last segment of webUrl (URL-decoded, accent-insensitive)
                web_url = drive.get("webUrl", "")
                if web_url:
                    url_segment = unquote(web_url.rstrip("/").rsplit("/", 1)[-1])
                    if _normalize_for_comparison(url_segment) == first_norm:
                        logger.info(
                            "Resolved SharePoint document library '%s' (webUrl='%s') → drive id '%s'",
                            first_segment,
                            web_url,
                            drive_id,
                        )
                        return f"{site_base}/drives/{drive_id}", (
                            f"/{rest}" if rest else "/"
                        )
    except Exception as e:
        logger.warning("Failed to list SharePoint drives: %s", format_exception_message(e))

    # No match — use default drive with the full path
    return f"{site_base}/drive", folder_path


async def sync_microsoft_drive(
    db: AsyncSession,
    kb: KnowledgeBase,
    user_id: uuid.UUID | str,
    folder_path: str = "/",
    site_id: str | None = None,
    integration_id: str | None = None,
    recursive: bool = False,
    *,
    sync_source_id: str | None = None,
) -> list[str]:
    """Sync files from OneDrive or SharePoint into a knowledge base (full-refresh).

    Args:
        folder_path: Path within the drive (default: root).  For SharePoint,
                     the path may start with a document library name (e.g.
                     ``/Documents partages/Sub/Folder``); the library name is
                     automatically resolved to the correct drive.  SharePoint
                     sharing URLs (e.g.
                     ``https://tenant.sharepoint.com/:f:/s/site/AAA?e=token``)
                     are also accepted and resolved automatically via the Graph
                     API ``/shares`` endpoint.
        site_id: SharePoint site ID or site name (if syncing from SharePoint
                 instead of OneDrive).  Accepts a full composite ID
                 (``hostname,col-guid,site-guid``) or a plain site name which
                 will be resolved via the Graph search API.
        integration_id: Explicit integration to use (optional).
        recursive: Whether to recurse into subfolders (default: False).
        sync_source_id: The :class:`KBSyncSource` behind this sync, owner
            of the documents it imports; None for a one-shot import.

    Returns list of all synced document IDs.
    """
    token = await _get_integration_token(
        db, user_id, "microsoft", integration_id=integration_id
    )
    headers = {"Authorization": f"Bearer {token}"}

    all_doc_ids: set[str] = set()
    embed_doc_ids: list[str] = []

    if _is_sharepoint_sharing_url(folder_path):
        # Sharing URLs (https://tenant.sharepoint.com/:f:/s/...) cannot be
        # used as path segments in the Graph API.  Resolve them via the
        # /shares endpoint to get the actual driveId + itemId.
        async with egress_client(timeout=30) as resolve_client:
            base, start_url = await _resolve_drive_sharing_url(
                resolve_client,
                headers,
                folder_path,
            )
    elif site_id:
        async with egress_client(timeout=30) as resolve_client:
            resolved_site_id = await _resolve_sharepoint_site_id(
                resolve_client,
                headers,
                site_id,
            )
            base, folder_path = await _resolve_sharepoint_drive(
                resolve_client,
                headers,
                resolved_site_id,
                folder_path,
            )
        if folder_path == "/" or not folder_path:
            start_url = f"{base}/root/children"
        else:
            start_url = f"{base}/root:/{folder_path.strip('/')}:/children"
    else:
        base = f"{GRAPH_API}/me/drive"
        if folder_path == "/" or not folder_path:
            start_url = f"{base}/root/children"
        else:
            start_url = f"{base}/root:/{folder_path.strip('/')}:/children"

    supported_exts = {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".pdf",
        ".docx",
        ".doc",
        ".html",
        ".htm",
        ".xml",
        ".py",
        ".js",
        ".ts",
        ".yaml",
        ".yml",
        ".toml",
    }

    from collections import deque

    async with egress_client(timeout=60) as client:
        # BFS queue of folder listing URLs to process (recursive)
        folder_queue: deque[str] = deque([start_url])

        while folder_queue:
            next_url: str | None = folder_queue.popleft()
            while next_url:
                resp = await client.get(next_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                items = data.get("value", [])
                next_url = data.get("@odata.nextLink")

                for item in items:
                    name = item.get("name", "")

                    # Recurse into subfolders (only when recursive is enabled)
                    if "folder" in item:
                        if recursive:
                            item_id = item.get("id")
                            if item_id:
                                folder_queue.append(f"{base}/items/{item_id}/children")
                        continue

                    ext = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
                    if ext not in supported_exts:
                        continue
                    size = item.get("size", 0)
                    if size > _MAX_DRIVE_FILE_SIZE_BYTES:
                        continue

                    try:
                        item_id = item.get("id")
                        download_url = item.get("@microsoft.graph.downloadUrl")
                        if not download_url:
                            if not item_id:
                                continue
                            dl_resp = await client.get(
                                f"{base}/items/{item_id}/content",
                                headers=headers,
                                follow_redirects=True,
                            )
                            raw = dl_resp.content
                        else:
                            dl_resp = await client.get(download_url)
                            raw = dl_resp.content

                        from app.services.chunking import extract_text_from_file_async

                        # Off-loop: Drive syncs run in the WEB process
                        # scheduler tick; an inline PDF parse froze the
                        # event loop and failed liveness.
                        content = await extract_text_from_file_async(raw, name)

                        # Use the parent path in the filename to avoid collisions
                        parent_path = item.get("parentReference", {}).get("path", "")
                        # parentReference.path looks like "/drive/root:/folder/subfolder"
                        # Strip the drive prefix to get a clean relative path
                        if ":/" in parent_path:
                            relative_dir = parent_path.split(":/", 1)[1]
                        elif parent_path:
                            relative_dir = parent_path.strip("/")
                        else:
                            relative_dir = ""
                        display_name = (
                            f"{relative_dir}/{name}" if relative_dir else name
                        )

                        web_url = item.get("webUrl", "")
                        async with db.begin_nested():
                            doc, needs_embed = await _upsert_document(
                                db,
                                kb,
                                display_name,
                                content,
                                "drive",
                                source_url=web_url,
                                sync_source_id=sync_source_id,
                            )
                            if doc:
                                all_doc_ids.add(str(doc.id))
                                if needs_embed:
                                    embed_doc_ids.append(str(doc.id))

                    except Exception as e:
                        logger.warning(
                            "Failed to sync Drive file %s: %s", name, format_exception_message(e)
                        )

    deleted = await _delete_stale_documents(
        db, kb, "drive", all_doc_ids, sync_source_id=sync_source_id
    )
    await _recount_kb_stats(db, kb, content_changed=bool(embed_doc_ids or deleted))
    await db.commit()
    await enqueue_documents(embed_doc_ids)

    if deleted:
        logger.info(
            "OneDrive sync: removed %d stale document(s) from KB '%s'", deleted, kb.name
        )
    return list(all_doc_ids)


# ---------------------------------------------------------------------------
# Google Drive sync
# ---------------------------------------------------------------------------


async def sync_google_drive(
    db: AsyncSession,
    kb: KnowledgeBase,
    user_id: uuid.UUID | str,
    folder_id: str = "root",
    integration_id: str | None = None,
    *,
    sync_source_id: str | None = None,
) -> list[str]:
    """Sync files from Google Drive into a knowledge base (full-refresh).

    Args:
        folder_id: Google Drive folder ID (default: 'root' for My Drive).
        integration_id: Explicit integration to use (optional).
        sync_source_id: The :class:`KBSyncSource` behind this sync, owner
            of the documents it imports; None for a one-shot import.

    Returns list of all synced document IDs.
    """
    token = await _get_integration_token(
        db, user_id, "google", integration_id=integration_id
    )
    headers = {"Authorization": f"Bearer {token}"}

    all_doc_ids: set[str] = set()
    embed_doc_ids: list[str] = []

    supported_mimes = {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "application/json",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    google_export = {
        "application/vnd.google-apps.document": ("text/plain", ".txt"),
        "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
        "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    }

    from collections import deque

    async with egress_client(timeout=60) as client:
        # BFS queue: (folder_id, path_prefix) for recursive traversal
        folder_queue: deque[tuple[str, str]] = deque([(folder_id, "")])

        while folder_queue:
            current_folder_id, path_prefix = folder_queue.popleft()
            query = f"'{current_folder_id}' in parents and trashed = false"
            page_token: str | None = None
            while True:
                params: dict = {
                    "q": query,
                    "fields": "nextPageToken,files(id,name,mimeType,size,webViewLink)",
                    "pageSize": 100,
                }
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(
                    f"{DRIVE_API}/files",
                    headers=headers,
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                files = data.get("files", [])
                page_token = data.get("nextPageToken")

                for f in files:
                    name = f.get("name", "")
                    mime = f.get("mimeType", "")
                    file_id = f.get("id")
                    if not file_id:
                        continue

                    # Recurse into subfolders
                    if mime == "application/vnd.google-apps.folder":
                        subfolder_prefix = (
                            f"{path_prefix}{name}/" if path_prefix else f"{name}/"
                        )
                        folder_queue.append((file_id, subfolder_prefix))
                        continue

                    try:
                        if mime in google_export:
                            export_mime, ext = google_export[mime]
                            export_resp = await client.get(
                                f"{DRIVE_API}/files/{file_id}/export",
                                headers=headers,
                                params={"mimeType": export_mime},
                            )
                            export_resp.raise_for_status()
                            content = export_resp.text
                            doc_name = (
                                f"{name}{ext}" if not name.endswith(ext) else name
                            )

                        elif mime in supported_mimes:
                            size = int(f.get("size", 0))
                            if size > _MAX_DRIVE_FILE_SIZE_BYTES:
                                continue
                            dl_resp = await client.get(
                                f"{DRIVE_API}/files/{file_id}",
                                headers=headers,
                                params={"alt": "media"},
                            )
                            dl_resp.raise_for_status()

                            if mime == "application/pdf" or mime.endswith("document"):
                                from app.services.chunking import (
                                    extract_text_from_file_async,
                                )

                                # Off-loop: see Drive sync above.
                                content = await extract_text_from_file_async(
                                    dl_resp.content, name
                                )
                            else:
                                content = dl_resp.text
                            doc_name = name
                        else:
                            continue

                        # Include path prefix for documents in subfolders
                        display_name = (
                            f"{path_prefix}{doc_name}" if path_prefix else doc_name
                        )

                        web_url = f.get("webViewLink", "")
                        async with db.begin_nested():
                            doc, needs_embed = await _upsert_document(
                                db,
                                kb,
                                display_name,
                                content,
                                "google_drive",
                                source_url=web_url,
                                sync_source_id=sync_source_id,
                            )
                            if doc:
                                all_doc_ids.add(str(doc.id))
                                if needs_embed:
                                    embed_doc_ids.append(str(doc.id))

                    except Exception as e:
                        logger.warning(
                            "Failed to sync Google Drive file %s: %s",
                            name,
                            format_exception_message(e),
                        )

                if not page_token:
                    break

    deleted = await _delete_stale_documents(
        db, kb, "google_drive", all_doc_ids, sync_source_id=sync_source_id
    )
    await _recount_kb_stats(db, kb, content_changed=bool(embed_doc_ids or deleted))
    await db.commit()
    await enqueue_documents(embed_doc_ids)

    if deleted:
        logger.info(
            "Google Drive sync: removed %d stale document(s) from KB '%s'",
            deleted,
            kb.name,
        )
    return list(all_doc_ids)


# ---------------------------------------------------------------------------
# GitHub repository sync
# ---------------------------------------------------------------------------


# Branches eligible for automatic default-branch recovery on a 404 tree
# fetch. A configured "main"/"master" is almost always a stale default
# (the repo was renamed the other way); an explicitly configured
# non-default branch (e.g. "develop") must fail fast instead of silently
# syncing different content from the default branch.
_COMMON_DEFAULT_BRANCHES = frozenset({"main", "master"})


async def _resolve_github_default_branch(
    client: httpx.AsyncClient,
    repo: str,
    headers: dict,
) -> str | None:
    """Fetch the actual default branch of a GitHub repository.

    Calls ``GET /repos/{repo}`` and returns the ``default_branch`` field.
    Returns ``None`` when the branch cannot be determined (request failed,
    non-200 response, or missing/blank field) so the caller can skip the
    retry instead of guessing — the original 404 then propagates with the
    branch the user actually configured (same fail-don't-guess semantics
    as the proactive resolution in the GitHub integration tools).
    """
    try:
        resp = await client.get(f"{GITHUB_API}/repos/{repo}", headers=headers)
        if resp.status_code == 200:
            resolved = resp.json().get("default_branch")
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
    except Exception as exc:
        logger.warning(
            "Could not resolve default_branch for %s: %s", repo, format_exception_message(exc)
        )
    return None


async def _persist_resolved_default_branch(
    db: AsyncSession,
    source_id: str,
    resolved_branch: str,
) -> None:
    """Write the resolved default branch back to the sync source config.

    Without this, the main/master 404 recovery in :func:`sync_github_repo`
    re-resolves the default branch on EVERY sync — one extra GitHub API call
    and one warning log per sync, forever. Persisting the resolved value makes
    the next sync hit the right branch directly. Best-effort: a failure here
    only means the next sync re-resolves again.
    """
    try:
        source = await db.get(KBSyncSource, uuid.UUID(str(source_id)))
        if source is None:
            return
        # Plain JSON column — reassign a fresh dict so the change is tracked.
        config = dict(source.config or {})
        config["branch"] = resolved_branch
        source.config = config
        await db.flush()
    except Exception as exc:
        logger.warning(
            "Could not persist resolved default branch %r for sync source %s: %s",
            resolved_branch,
            source_id,
            format_exception_message(exc),
        )


async def sync_github_repo(
    db: AsyncSession,
    kb: KnowledgeBase,
    user_id: uuid.UUID | str,
    repo: str,
    branch: str = "main",
    path: str = "",
    integration_id: str | None = None,
    *,
    public: bool = False,
    source_id: str | None = None,
    statement_timeout_secs: int | None = None,
) -> list[str]:
    """Sync files from a GitHub repository into a knowledge base (full-refresh).

    Fetches ALL matching files from the repo (no arbitrary limit).  Documents
    whose content changed are updated and re-embedded.  Files that no longer
    exist in the source are deleted from the KB.

    Args:
        repo: Full repo name (e.g. 'owner/repo-name') or full GitHub URL.
        branch: Branch to sync from (default: 'main').  If the configured
            branch is ``main`` or ``master`` and the initial tree fetch
            returns 404 (e.g. the repo's default branch has been renamed from
            ``master`` to ``main`` or vice-versa), the function resolves the
            repository's actual ``default_branch`` via the GitHub API and
            retries automatically.  Explicitly configured non-default
            branches are never substituted — their 404 propagates as-is.
        path: Optional subdirectory within the repo.
        integration_id: Explicit integration to use (optional).
        public: If True, access the repo without authentication (public repos
            only).  Uses raw.githubusercontent.com for file content to avoid
            unauthenticated API rate limits.
        source_id: The :class:`KBSyncSource` id behind this sync (scheduler
            path only): the owner of the documents it imports, whose stale
            sweep leaves the other GitHub sources' documents alone.
            When the default-branch recovery kicks in, the resolved branch is
            persisted back to the source config so the next sync uses it
            directly instead of re-resolving every time.
        statement_timeout_secs: The elevated per-transaction
            ``statement_timeout`` the scheduler applied to this sync session
            (see :func:`_apply_sync_statement_timeout`).  Only used to
            RE-apply the timeout after a session-recovery rollback in the
            per-file loop — the ``SET LOCAL`` override dies with the
            rolled-back transaction.  ``None`` (manual API path) keeps the
            pool default.

    Returns list of all synced document IDs.
    """
    repo = _normalize_github_repo(repo)
    # Branch comes from a free-form sync-source config dict; normalize it the
    # same way the repo is normalized so a padded value (e.g. "master ") does
    # not build a 404 URL that also fails the _COMMON_DEFAULT_BRANCHES gate,
    # and a blank value falls back to the documented default. A non-string
    # value (the config is user-entered JSON) is treated as blank so a
    # scheduled sync degrades to the default instead of crashing with an
    # AttributeError before any GitHub call is made.
    if not isinstance(branch, str):
        branch = ""
    branch = branch.strip() or "main"

    if public:
        headers = {"Accept": "application/vnd.github.v3+json"}
    else:
        token = await _get_integration_token(
            db, user_id, "github", integration_id=integration_id
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    all_doc_ids: set[str] = set()
    embed_doc_ids: list[str] = []
    session_recovered = False

    supported_exts = {
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".java",
        ".rb",
        ".php",
        ".c",
        ".cpp",
        ".h",
        ".cs",
        ".swift",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".xml",
        ".html",
        ".css",
        ".sh",
        ".bash",
        ".dockerfile",
        ".tf",
        ".sql",
        ".rst",
        ".adoc",
        ".org",
    }

    async with egress_client(timeout=60, follow_redirects=True) as client:
        # Get the full recursive file tree (single API call)
        api_path = path.strip("/") if path else ""
        url = f"{GITHUB_API}/repos/{repo}/git/trees/{branch}?recursive=1"

        resp = await client.get(url, headers=headers)
        if resp.status_code == 404 and branch in _COMMON_DEFAULT_BRANCHES:
            # The configured branch may be a stale default (e.g. the repo's
            # default branch was renamed from "master" to "main" or
            # vice-versa). Resolve the actual default branch from the repo
            # metadata and retry once. Restricted to main/master so an
            # explicitly configured non-default branch (e.g. "develop")
            # fails fast instead of silently syncing the default branch.
            # When resolution fails or the resolved branch equals the
            # configured one (repo genuinely absent), the original 404
            # propagates unchanged below.
            resolved = await _resolve_github_default_branch(client, repo, headers)
            if resolved and resolved != branch:
                logger.warning(
                    "GitHub tree fetch 404 for branch %r on %s (KB '%s', "
                    "kb_id=%s) — retrying with resolved default_branch %r",
                    branch,
                    repo,
                    kb.name,
                    kb.id,
                    resolved,
                )
                if source_id:
                    # Persist so the next scheduled sync uses the resolved
                    # branch directly instead of re-resolving on every run.
                    await _persist_resolved_default_branch(db, source_id, resolved)
                branch = resolved
                url = f"{GITHUB_API}/repos/{repo}/git/trees/{branch}?recursive=1"
                resp = await client.get(url, headers=headers)
        # Checked after the 404 recovery so it covers both the initial
        # response and the retried one (an empty repo can 409 either way).
        if resp.status_code == 409:
            logger.info("GitHub repo %s is empty (409) — skipping sync", repo)
            return []
        if resp.status_code != 200:
            body = resp.text[:500]
            logger.error(
                "GitHub API %s for %s – body: %s | rate-limit-remaining: %s",
                resp.status_code,
                url,
                body,
                resp.headers.get("x-ratelimit-remaining", "?"),
            )
            resp.raise_for_status()
        tree = resp.json().get("tree", [])

        # Filter to supported files within the target path — no arbitrary limit
        files = []
        for item in tree:
            if item.get("type") != "blob":
                continue
            fpath = item.get("path")
            if not fpath:
                continue
            if api_path and not fpath.startswith(api_path):
                continue
            ext = f".{fpath.rsplit('.', 1)[-1].lower()}" if "." in fpath else ""
            if ext in supported_exts:
                size = item.get("size", 0)
                if size <= _MAX_GITHUB_FILE_SIZE_BYTES:
                    files.append(item)

        logger.info(
            "GitHub sync: found %d matching files in %s/%s",
            len(files),
            repo,
            api_path or "(root)",
        )

        # Fetch content for every matching file.
        # Use savepoints so a single-file failure doesn't roll back the
        # whole transaction (which would expire all ORM objects and cause
        # MissingGreenlet / xd2s errors on subsequent iterations).
        for item in files:
            fpath = item["path"]
            try:
                if public:
                    # Use raw.githubusercontent.com (no API rate limit)
                    file_url = (
                        f"https://raw.githubusercontent.com/{repo}/{branch}/{fpath}"
                    )
                    file_headers = None
                else:
                    file_url = f"{GITHUB_API}/repos/{repo}/contents/{fpath}?ref={branch}"
                    file_headers = {
                        **headers,
                        "Accept": "application/vnd.github.v3.raw",
                    }
                # Transient 5xx / 429 / network errors are retried with
                # backoff; permanent 4xx and exhausted retries both come back
                # as ``resp is None`` (the helper already logged the reason),
                # so the file is skipped without failing the whole sync.
                file_resp, is_permanent, _throttled = await _fetch_with_retry(
                    client,
                    file_url,
                    max_attempts=_GITHUB_FILE_RETRY_ATTEMPTS,
                    base_delay=_GITHUB_FILE_RETRY_BASE_DELAY,
                    headers=file_headers,
                    log_label="GitHub sync",
                )
                if file_resp is None:
                    # Permanent 4xx are skipped without retrying, so the two
                    # outcomes get distinct wording (the helper's own log
                    # carries the status / error detail).
                    logger.warning(
                        "Failed to sync GitHub file %s: %s",
                        fpath,
                        (
                            "permanent fetch failure"
                            if is_permanent
                            else "fetch failed after retries"
                        ),
                    )
                    continue
                content = file_resp.text
                await asyncio.sleep(_GITHUB_FILE_FETCH_DELAY)

                github_url = f"https://github.com/{repo}/blob/{branch}/{fpath}"
                async with db.begin_nested():
                    doc, needs_embed = await _upsert_document(
                        db,
                        kb,
                        fpath,
                        content,
                        "github",
                        source_url=github_url,
                        sync_source_id=source_id,
                    )
                    if doc:
                        all_doc_ids.add(str(doc.id))
                        if needs_embed:
                            embed_doc_ids.append(str(doc.id))

            except Exception as e:
                logger.warning(
                    "Failed to sync GitHub file %s: %s", fpath, format_exception_message(e)
                )
                # The begin_nested() savepoint should have rolled back cleanly,
                # but if the savepoint rollback itself failed (e.g. the DB
                # connection was lost mid-write), the outer transaction is left
                # in a deactivated "aborted" state.  Every subsequent file would
                # then immediately fail with the unhelpful cascade error
                # "Can't reconnect until invalid transaction is rolled back"
                # (SQLAlchemy PendingRollbackError), masking the original error
                # and causing the whole sync run to time out.
                # In that state the server-side transaction is already gone, so
                # a full rollback() loses no recoverable work — it only resets
                # the Python-side session so the remaining files can sync (the
                # rolled-back files are re-upserted on the next sync run via
                # their content_hash mismatch).  In the common case where the
                # savepoint recovered cleanly the session is still active, so
                # the rollback is skipped and the accumulated (uncommitted)
                # progress from earlier files is preserved.
                if not db.sync_session.is_active:
                    try:
                        await db.rollback()
                        session_recovered = True
                        # The rollback discarded the uncommitted upserts of the
                        # earlier files in this batch — drop their pending
                        # embedding ids so enqueue_documents() doesn't enqueue
                        # rows that no longer exist (new docs) or were never
                        # updated (rolled-back updates).  all_doc_ids is
                        # intentionally kept: for updated docs the pre-existing
                        # row survives the rollback and its id must stay in the
                        # keep-set so _delete_stale_documents doesn't remove it
                        # (ids of rolled-back NEW docs are harmless there — the
                        # keep-set only protects rows that exist).
                        embed_doc_ids.clear()
                        # rollback() ended the transaction, which also reset
                        # the transaction-scoped (SET LOCAL) statement_timeout
                        # the scheduler raised for this sync session — re-apply
                        # it so the remaining large-content upserts and the
                        # final stale-delete don't get cancelled at the 30s
                        # pool default.  No-op on the manual API path
                        # (statement_timeout_secs is None there).
                        await _apply_sync_statement_timeout(db, statement_timeout_secs)
                        # rollback() also expired every ORM instance in the
                        # session (unconditionally — expire_on_commit only
                        # affects commit), so the kb.id / kb.name reads on the
                        # remaining iterations would raise MissingGreenlet on
                        # the async driver.  Eagerly refresh `kb` to make it
                        # usable again.
                        await db.refresh(kb)
                    except Exception as recovery_exc:
                        # Best-effort: a second failure here means the
                        # connection is truly gone; subsequent files (and the
                        # final commit) will surface their own errors.
                        logger.warning(
                            "GitHub sync: failed to recover DB session after "
                            "per-file error on %s: %s",
                            fpath,
                            format_exception_message(recovery_exc),
                        )

    # Remove docs that no longer exist in the repo
    deleted = await _delete_stale_documents(
        db, kb, "github", all_doc_ids, sync_source_id=source_id
    )
    if session_recovered and all_doc_ids:
        # A mid-loop recovery rollback discarded the uncommitted upserts of
        # the files processed before the failure.  Ids of rolled-back NEW
        # docs still sit in all_doc_ids — kept there on purpose, because the
        # stale-delete keep-set above must protect the surviving rows of
        # rolled-back UPDATED docs — but they must not leak into the returned
        # document ids (SyncResponse.document_ids) or inflate the
        # documents_synced / last_sync_documents counts.  After the
        # stale-delete, the GitHub docs remaining in the DB are exactly the
        # persisted members of all_doc_ids, so one id query re-grounds the
        # returned set (rebinding, not in-place, so the keep-set passed to
        # _delete_stale_documents is unaffected).
        persisted_result = await db.execute(
            select(KnowledgeDocument.id).where(
                KnowledgeDocument.knowledge_base_id == kb.id,
                KnowledgeDocument.source_type == "github",
            )
        )
        all_doc_ids = all_doc_ids & {str(did) for did in persisted_result.scalars()}
    await _recount_kb_stats(db, kb, content_changed=bool(embed_doc_ids or deleted))
    await db.commit()
    await enqueue_documents(embed_doc_ids)

    if deleted:
        logger.info(
            "GitHub sync: removed %d stale document(s) from KB '%s'", deleted, kb.name
        )
    logger.info(
        "GitHub sync for KB '%s': %d total files, %d new/updated, %d removed",
        kb.name,
        len(all_doc_ids),
        len(embed_doc_ids),
        deleted,
    )
    return list(all_doc_ids)


# ---------------------------------------------------------------------------
# Website scraping sync
# ---------------------------------------------------------------------------


def _is_permanent_http_status(status_code: int) -> bool:
    """Classify an HTTP status as a permanent (non-retryable) failure.

    Permanent: 4xx client errors that won't change on retry (404 not found,
    403 forbidden, 401 unauthorized, …). Transient (and therefore NOT
    permanent): 408 request timeout and 425 too-early (timing), 429 rate-limit,
    and any 5xx server error.
    """
    return 400 <= status_code < 500 and status_code not in (408, 425, 429)


# httpx request errors that won't change on retry: a malformed/unsupported URL
# (bad scheme or unparseable) or a redirect loop is a configuration problem, not
# a transient network blip. ``InvalidURL`` is NOT an ``HTTPError`` subclass, so
# catching it here also stops a bad URL from escaping the retry handler and
# aborting the whole sync.
_PERMANENT_REQUEST_ERRORS = (
    httpx.UnsupportedProtocol,
    httpx.TooManyRedirects,
    httpx.InvalidURL,
)


async def _fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_attempts: int = _WEBSITE_RETRY_ATTEMPTS,
    base_delay: float = _WEBSITE_RETRY_BASE_DELAY,
    headers: dict | None = None,
    log_label: str = "Website sync",
) -> tuple[httpx.Response | None, bool, bool]:
    """Fetch a URL with exponential backoff + jitter on transient failures.

    ``headers`` (when provided) are forwarded to every attempt so
    authenticated callers (GitHub file fetches) can reuse the same
    transient-retry policy as the website crawler. ``log_label`` prefixes the
    log lines so a GitHub retry doesn't masquerade as a website crawl.

    Returns ``(response, is_permanent_failure, observed_transient)``:

    * ``(resp, False, throttled)`` — success. ``throttled`` is ``True`` when the
      fetch only succeeded *after* at least one transient failure / back-off, so
      the caller can hold (rather than relax) its adaptive inter-request delay
      and converge on a rate the origin tolerates instead of oscillating back
      into the limiter.
    * ``(None, True, …)`` — a *permanent* failure (4xx except 408/425/429): the
      page is gone or forbidden, so the crawler should count it toward the
      permanent-abort threshold and never retry it.
    * ``(None, False, True)`` — a *transient* failure (429 / 5xx / 408 / 425 /
      network) that survived all retries: the caller should slow down (adaptive
      throttle) and keep crawling other pages rather than abort.
    """
    observed_transient = False

    def _retry_wait(attempt_index: int, *, retry_after: float = 0.0) -> float:
        backoff = base_delay * (2**attempt_index)
        # Jitter spreads retries so a fleet of crawlers doesn't synchronise its
        # back-off and hammer the origin in lockstep. ``secrets`` avoids ruff's
        # non-crypto-random warning; the value isn't security-sensitive.
        jitter = (secrets.randbelow(1000) / 1000.0) * base_delay if base_delay else 0.0
        # Apply jitter *after* the Retry-After floor: otherwise a large
        # server-provided Retry-After dominates ``max(...)`` and the jitter is
        # discarded, re-synchronising every crawler onto the same wake-up time.
        # The result is still >= retry_after (the floor is honoured) but
        # de-correlated.
        return max(retry_after, backoff) + jitter

    # Only forward ``headers`` when explicitly provided so plain callers (and
    # the minimal test doubles that implement ``get(url)``) are unaffected.
    request_kwargs: dict = {"headers": headers} if headers is not None else {}

    for attempt in range(max_attempts):
        is_last = attempt >= max_attempts - 1
        try:
            resp = await client.get(url, **request_kwargs)
            status = resp.status_code

            if _is_permanent_http_status(status):
                logger.warning(
                    "%s: permanent failure fetching %s (status %d)",
                    log_label,
                    url,
                    status,
                )
                return None, True, observed_transient

            if status == 429 or status == 408 or status == 425 or status >= 500:
                observed_transient = True
                if is_last:
                    logger.warning(
                        "%s: failed to fetch %s after %d attempts (status %d)",
                        log_label,
                        url,
                        max_attempts,
                        status,
                    )
                    return None, False, observed_transient
                retry_after = (
                    _parse_retry_after_seconds(resp.headers.get("Retry-After"))
                    if status == 429
                    else 0.0
                )
                wait = _retry_wait(attempt, retry_after=retry_after)
                logger.info(
                    "%s: %d for %s — retrying in %.1fs", log_label, status, url, wait
                )
                await asyncio.sleep(wait)
                continue

            # Every 4xx/5xx is already classified above (permanent → return,
            # transient → retry/return), so the status is < 400 here and the
            # response is good. (A raise_for_status() here would be dead code; any
            # future status that slips through still falls to the generic
            # httpx.HTTPError handler below and is treated as transient.)
            return resp, False, observed_transient
        except _PERMANENT_REQUEST_ERRORS as exc:
            # Malformed/unsupported URL or a redirect loop — won't change on
            # retry. Classify as permanent so we don't waste retries, don't trip
            # the transient-abort valve, and don't emit misleading "network
            # error" logs.
            logger.warning(
                "%s: permanent request error fetching %s: %s",
                log_label,
                url,
                format_exception_message(exc),
            )
            return None, True, observed_transient
        except httpx.HTTPError as e:
            # Connection reset / timeout / DNS — transient: retry, then give up
            # on this page WITHOUT counting it as permanent.
            observed_transient = True
            if not is_last:
                wait = _retry_wait(attempt)
                logger.info(
                    "%s: network error fetching %s: %s — retrying in %.1fs",
                    log_label,
                    url,
                    format_exception_message(e),
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            logger.warning(
                "%s: network error fetching %s after %d attempts: %s",
                log_label,
                url,
                max_attempts,
                format_exception_message(e),
            )
            return None, False, observed_transient
    logger.warning("%s: exhausted retries for %s", log_label, url)
    return None, False, observed_transient


# ---------------------------------------------------------------------------
# Drata policies sync
# ---------------------------------------------------------------------------


async def sync_drata_policies(
    db: AsyncSession,
    kb: KnowledgeBase,
    user_id: uuid.UUID | str,
    statuses: list[str] | None = None,
    integration_id: str | None = None,
    *,
    sync_source_id: str | None = None,
) -> list[str]:
    """Sync policies from Drata into a knowledge base (full-refresh).

    For each published policy, fetches the current version's HTML content
    and stores it as a markdown document in the KB.

    Args:
        statuses: Optional list of policy statuses to include (default: ACTIVE only).
        integration_id: Explicit integration to use (optional).
        sync_source_id: The :class:`KBSyncSource` behind this sync, owner
            of the documents it imports.

    Returns list of all synced document IDs.
    """
    token = await _get_integration_token(
        db, user_id, "drata", integration_id=integration_id
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    if not statuses:
        statuses = ["ACTIVE"]

    all_doc_ids: set[str] = set()
    embed_doc_ids: list[str] = []

    # Resolve region-specific base URL from the integration's extra_config
    from app.integrations.providers.drata import DEFAULT_BASE_URL, resolve_base_url

    drata_base_url = DEFAULT_BASE_URL
    try:
        integ = await _get_drata_integration(db, user_id, integration_id)
        if integ and integ.extra_config_enc:
            extra = await reveal_json_field(integ, "extra_config_enc") or {}
            region = extra.get("region", "").strip()
            if region:
                drata_base_url = resolve_base_url(region)
    except Exception as exc:
        logger.warning(
            "Could not resolve Drata region — using default US endpoint: %s",
            format_exception_message(exc),
        )

    async with egress_client(timeout=60) as client:
        # 1. Fetch all policies (paginated)
        policies: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict = {
                "size": _DRATA_PAGE_SIZE,
                "expand[]": ["owner"],
                "statuses[]": statuses,
            }
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                f"{drata_base_url}/policies", headers=headers, params=params
            )
            resp.raise_for_status()
            data = resp.json()
            policies.extend(data.get("data", []))
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor:
                break

        logger.info("Drata sync: found %d policies", len(policies))

        # 2. For each policy, fetch the current version content
        for policy in policies:
            policy_id = policy.get("id")
            policy_name = policy.get("name", "Untitled")
            current_version_id = policy.get("currentVersionId")

            if not policy_id:
                continue

            try:
                # Build policy metadata header
                owner = policy.get("owner", {})
                owner_name = (
                    f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip()
                    if owner
                    else "N/A"
                )

                meta_lines = [
                    f"# {policy_name}",
                    "",
                    f"**Status:** {policy.get('status', 'N/A')}",
                    f"**Version:** {policy.get('version', '?')}.{policy.get('subVersion', '0')}",
                    f"**Owner:** {owner_name}",
                    f"**Scope:** {policy.get('scope', 'N/A')}",
                    f"**Published:** {policy.get('publishedAt', 'N/A')}",
                    f"**Approved:** {policy.get('approvedAt', 'N/A')}",
                    f"**Renewal Date:** {policy.get('renewalDate', 'N/A')}",
                    "",
                    "---",
                    "",
                ]

                # Try to fetch the HTML content of the current version
                body_content = ""
                if current_version_id:
                    try:
                        content_resp = await client.get(
                            f"{drata_base_url}/policies/{policy_id}/policy-versions/{current_version_id}",
                            headers={**headers, "Accept": "text/html"},
                        )
                        if content_resp.status_code == 200:
                            content_type = content_resp.headers.get("content-type", "")
                            if "text/html" in content_type:
                                # Convert HTML to readable text
                                html = content_resp.text
                                # Basic HTML → markdown-ish text
                                text = re.sub(r"<br\s*/?>", "\n", html)
                                text = re.sub(r"<h[1-6][^>]*>", "\n## ", text)
                                text = re.sub(r"</h[1-6]>", "\n", text)
                                text = re.sub(r"<li[^>]*>", "- ", text)
                                text = re.sub(r"<p[^>]*>", "\n", text)
                                text = re.sub(r"</p>", "\n", text)
                                text = re.sub(r"<[^>]+>", "", text)
                                text = re.sub(r"\n{3,}", "\n\n", text)
                                body_content = text.strip()
                            else:
                                # JSON fallback — try description field
                                version_data = content_resp.json()
                                body_content = version_data.get("description", "")
                    except Exception as e:
                        logger.warning(
                            "Failed to fetch content for policy %s version %s: %s",
                            policy_id,
                            current_version_id,
                            format_exception_message(e),
                        )

                content = "\n".join(meta_lines) + (
                    body_content or "(Policy content not available)"
                )

                # Use a stable source_url
                source_url = f"https://app.drata.com/policy-center/{policy_id}"

                filename = f"{policy_name}.md"
                if len(filename) > _MAX_FILENAME_LENGTH:
                    filename = filename[:147] + ".md"

                async with db.begin_nested():
                    doc, needs_embed = await _upsert_document(
                        db,
                        kb,
                        filename,
                        content,
                        "drata",
                        source_url=source_url,
                        sync_source_id=sync_source_id,
                    )
                    if doc:
                        all_doc_ids.add(str(doc.id))
                        if needs_embed:
                            embed_doc_ids.append(str(doc.id))

            except Exception as e:
                logger.warning(
                    "Failed to sync Drata policy %s (%s): %s",
                    policy_id,
                    policy_name,
                    format_exception_message(e),
                )

    deleted = await _delete_stale_documents(
        db, kb, "drata", all_doc_ids, sync_source_id=sync_source_id
    )
    await _recount_kb_stats(db, kb, content_changed=bool(embed_doc_ids or deleted))
    await db.commit()
    await enqueue_documents(embed_doc_ids)

    if deleted:
        logger.info(
            "Drata sync: removed %d stale document(s) from KB '%s'", deleted, kb.name
        )
    logger.info(
        "Drata sync for KB '%s': %d policies total, %d new/updated, %d removed",
        kb.name,
        len(all_doc_ids),
        len(embed_doc_ids),
        deleted,
    )
    return list(all_doc_ids)


async def sync_website(
    db: AsyncSession,
    kb: KnowledgeBase,
    url: str,
    max_pages: int = _DEFAULT_MAX_WEBSITE_PAGES,
    max_depth: int = 2,
    statement_timeout_secs: int | None = None,
    *,
    sync_source_id: str | None = None,
) -> list[str]:
    """Crawl a website and import pages into a knowledge base (full-refresh).

    Starts from *url*, discovers internal links (same domain), and follows
    them up to *max_depth* levels.  Each page is extracted using trafilatura
    for high-quality main-content extraction (strips nav, ads, boilerplate).

    No integration required — works with any public website.

    Unlike the other sync sources, each crawled page is committed in its own
    short-lived session (and the stale cleanup runs in another) rather than the
    caller's ``db`` transaction, so the long network-bound crawl never holds a
    single transaction / row locks open. Those fresh sessions therefore need
    the elevated ``statement_timeout`` applied explicitly via
    *statement_timeout_secs* — otherwise large-content INSERTs and bulk DELETEs
    fall back to the 30s pool default and get cancelled (SQLSTATE 57014).

    Args:
        url: Starting URL (e.g. ``https://docs.example.com``).
        max_pages: Maximum number of pages to crawl (default 50, cap 500).
        max_depth: Maximum link-following depth (default 2, cap 5).
        statement_timeout_secs: Per-transaction ``statement_timeout`` to apply
            to the per-page and cleanup sessions. ``None`` keeps the pool
            default (used by direct callers / tests that don't sync large
            content).
        sync_source_id: The :class:`KBSyncSource` behind this sync, owner
            of the documents it imports.

    Returns list of all synced document IDs.

    Raises ValueError when the crawl produces no documents and at least one
    fetch failed with a permanent HTTP error, so the scheduler records the
    run as a failure instead of a zero-document success; the
    message embeds ``WEBSITE_ALL_PERMANENT_FAILURES_MARKER`` (feeding the
    perm-fail quarantine) only when no transient signal (failed fetch or
    throttled recovery) was observed in the same run.
    """
    from collections import deque
    from urllib.parse import urljoin, urlparse

    from app.core.parse_offload import run_parse
    from app.services.web_fetch import extract_from_html

    max_pages = min(max_pages, _MAX_WEBSITE_PAGES_CAP)
    max_depth = min(max_depth, _MAX_WEBSITE_DEPTH_CAP)

    # Normalize the URL the same way sync_github_repo normalizes its repo and
    # branch — the value comes from a free-form sync-source config
    # dict, so already-persisted bad rows are healed here too. Strip
    # surrounding whitespace (e.g. a URL pasted with a leading space) and
    # prepend "https://" when no explicit scheme is present (e.g.
    # "docs.example.com"). Both cases make httpx raise UnsupportedProtocol /
    # InvalidURL, which is classified as a permanent failure — silently
    # starving the KB of that source. A non-string value (the config
    # is user-entered JSON, so a persisted null/number reaches here as-is via
    # ``config.get("url", "")`` in the scheduler) is treated as blank — same
    # pattern as the non-string branch guard in sync_github_repo — so it hits
    # the fail-fast below instead of crashing with an AttributeError.
    if not isinstance(url, str):
        url = ""
    url = url.strip()
    if not url:
        # Fail fast: a blank URL can never crawl anything, and letting it run
        # would end with zero synced ids — the stale-cleanup pass below would
        # then delete every previously synced page of this source. Raising
        # surfaces a clear last_sync_error on the source instead.
        raise ValueError("Website source URL is empty")
    if not _URL_SCHEME_RE.match(url):
        url = "https://" + url
    # NOTE: unlike the agent-driven web_fetch tool, this crawl intentionally
    # does NOT go through web_fetch.validate_outbound_url — KB sync sources
    # are manage-gated and may legitimately point at internal mirrors or
    # airgapped hosts (see create_sync_source in api/v1/knowledge_bases.py).
    # Prepending "https://" grants nothing a fully-qualified URL could not
    # already reach on this path.

    parsed_base = urlparse(url)
    if parsed_base.scheme not in ("http", "https"):
        # Fail fast on explicit non-HTTP(S) schemes (e.g. "ftp://"): every
        # fetch would be a permanent UnsupportedProtocol failure that is only
        # logged — no exception propagates, so the run would be recorded as a
        # SUCCESS with zero synced ids and the stale-cleanup pass below would
        # delete every previously synced page of this source. Raising surfaces
        # a clear last_sync_error instead (same hazard as the blank/hostless
        # cases).
        raise ValueError(
            f"Website source URL scheme must be http or https, "
            f"got {parsed_base.scheme!r}"
        )
    base_domain = parsed_base.netloc.lower()
    if not base_domain:
        # Fail fast on hostless URLs (e.g. "https://" or "https:///path"):
        # they pass the scheme check above but can never fetch anything —
        # every attempt is a permanent InvalidURL failure, and finishing with
        # zero synced ids would let the stale-cleanup pass below delete every
        # previously synced page of this source (same hazard as the blank-URL
        # case). Raising surfaces a clear last_sync_error instead. Do NOT echo
        # the URL itself: the message is persisted to last_sync_error and
        # surfaced by the KB API, and a malformed URL's query/fragment could
        # carry tokens the viewer should not see.
        raise ValueError("Website source URL has no host")
    base_path = parsed_base.path.rstrip("/").lower()
    # Doubled pairs already present in the configured base URL are
    # legitimate for this site (e.g. github.com/grafana/grafana) and must
    # not trip the doubled-segment guard in the link-extraction loop below.
    base_doubled_segments = _doubled_path_segments(parsed_base.path)
    start_url = url.rstrip("/")

    visited: set[str] = set()
    all_doc_ids: set[str] = set()
    embed_doc_ids: list[str] = []
    bfs_queue: deque[tuple[str, int]] = deque([(start_url, 0)])

    skip_exts = frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".webp",
            ".pdf",
            ".zip",
            ".tar",
            ".gz",
            ".mp4",
            ".mp3",
            ".css",
            ".js",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
        }
    )

    consecutive_permanent_failures = 0
    consecutive_transient_failures = 0
    # Cumulative failure signals across the entire crawl.  Unlike the
    # *consecutive* counters above — which reset on any success and drive the
    # in-crawl abort — these never reset, so the post-loop guard can classify
    # a zero-document crawl: all-permanent failures feed the scheduler's
    # perm-fail quarantine, while any transient signal (a failed fetch OR a
    # throttled recovery on an otherwise successful fetch) downgrades the run
    # to a plain (non-permanent) failure so an outage-/rate-limit-tainted run
    # can never creep the strict perm-fail budget toward quarantine.
    total_permanent_failures = 0
    total_transient_failures = 0
    transient_signal_observed = False
    # Adaptive inter-request delay: grows when the origin rate-limits us and
    # relaxes after clean fetches, so we crawl at a rate the site tolerates
    # instead of tripping the limiter and aborting.
    request_delay = _WEBSITE_REQUEST_DELAY

    async with egress_client(
        follow_redirects=True,
        timeout=30,
        headers={"User-Agent": PLATFORM_USER_AGENT},
    ) as client:
        while bfs_queue and len(visited) < max_pages:
            if consecutive_permanent_failures >= _WEBSITE_CONSECUTIVE_PERMANENT_ABORT:
                logger.warning(
                    "Website sync: aborting crawl of %s after %d consecutive "
                    "permanent failures (4xx such as 404/403/401, or a "
                    "bad/unsupported URL) — likely a broken base URL or access "
                    "denied",
                    url,
                    consecutive_permanent_failures,
                )
                break
            if consecutive_transient_failures >= _WEBSITE_CONSECUTIVE_TRANSIENT_ABORT:
                logger.warning(
                    "Website sync: aborting crawl of %s after %d consecutive "
                    "transient failures — site appears persistently unavailable "
                    "or rate-limited despite back-off (now %.1fs between requests)",
                    url,
                    consecutive_transient_failures,
                    request_delay,
                )
                break

            current_url, depth = bfs_queue.popleft()

            normalised = current_url.rstrip("/")
            if normalised in visited:
                continue
            visited.add(normalised)

            resp, is_permanent_failure, throttled = await _fetch_with_retry(
                client, current_url
            )
            if resp is None:
                if is_permanent_failure:
                    consecutive_permanent_failures += 1
                    total_permanent_failures += 1
                    consecutive_transient_failures = 0
                else:
                    consecutive_transient_failures += 1
                    total_transient_failures += 1
                    transient_signal_observed = True
                    consecutive_permanent_failures = 0
                    # Back off the whole crawl so we stop tripping the limiter.
                    request_delay = min(
                        _WEBSITE_MAX_REQUEST_DELAY,
                        request_delay * _WEBSITE_THROTTLE_BACKOFF,
                    )
                # If this failure tripped an abort threshold, loop straight back so
                # the top-of-loop guard aborts now — don't sleep (up to the 8s cap)
                # just to give up on the next iteration.
                if (
                    consecutive_permanent_failures
                    >= _WEBSITE_CONSECUTIVE_PERMANENT_ABORT
                    or consecutive_transient_failures
                    >= _WEBSITE_CONSECUTIVE_TRANSIENT_ABORT
                ):
                    continue
                await asyncio.sleep(request_delay)
                continue

            consecutive_permanent_failures = 0
            consecutive_transient_failures = 0
            if throttled:
                # The fetch only succeeded after transient failures + back-off:
                # the run is outage-/rate-limit-tainted, so a zero-document
                # outcome must not be classified as all-permanent.
                transient_signal_observed = True
            else:
                # Relax the throttle toward the base delay only after a *clean*
                # fetch. If this page succeeded only after a back-off
                # (``throttled``), we're near the origin's limit — hold the
                # current delay instead of decaying it and oscillating straight
                # back into 429s.
                request_delay = max(
                    _WEBSITE_REQUEST_DELAY, request_delay * _WEBSITE_DELAY_DECAY
                )
            await asyncio.sleep(request_delay)

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                continue

            html = resp.text

            # Off-loop: trafilatura/BeautifulSoup extraction is
            # CPU-bound and this crawl loop runs in the WEB process scheduler
            # tick — an inline parse per page starves the event loop.
            # functools.partial (not a lambda) so the loop variables are bound
            # by value at call time (B023-safe).
            text, _ = await run_parse(
                functools.partial(extract_from_html, html, url=current_url)
            )

            if text and len(text.strip()) >= 20:
                url_path = urlparse(current_url).path.strip("/")
                filename = (url_path.replace("/", "_") or "index") + ".md"
                if len(filename) > _MAX_FILENAME_LENGTH:
                    filename = filename[:147] + ".md"

                try:
                    from app.core.database import async_session as _async_session

                    async with _async_session() as page_db:
                        # Each page commits in its own fresh session, so it must
                        # raise the elevated sync timeout itself — the caller's
                        # ``sync_db`` override does not reach here.
                        await _apply_sync_statement_timeout(
                            page_db, statement_timeout_secs
                        )
                        kb_ref = await page_db.get(KnowledgeBase, kb.id)
                        if not kb_ref:
                            continue
                        doc, needs_embed = await _upsert_document(
                            page_db,
                            kb_ref,
                            filename,
                            text,
                            "website",
                            source_url=current_url,
                            sync_source_id=sync_source_id,
                        )
                        if doc:
                            all_doc_ids.add(str(doc.id))
                            if needs_embed:
                                embed_doc_ids.append(str(doc.id))
                                # Each page commits on its own, before the
                                # final recount: its revision moves with it.
                                await bump_kb_revision(page_db, kb.id)
                        await page_db.commit()
                except Exception as e:
                    logger.warning(
                        "Failed to sync website page %s: %s",
                        current_url,
                        format_exception_message(e),
                    )

            if depth < max_depth:
                # Off-loop too: the link parse is a full HTML parse, as
                # CPU-bound as the extraction above.
                hrefs = await run_parse(functools.partial(_page_hrefs, html))
                for href in hrefs:
                    # Skip hrefs with a mangled scheme separator (e.g.
                    # "https;//cursor.com"): urljoin would glue them onto the
                    # current page path and the crawler would fetch a
                    # guaranteed-404 nonsense URL.
                    if _MALFORMED_HREF_SCHEME_RE.match(href):
                        logger.debug(
                            "Website sync: skipping malformed href %r on %s",
                            href,
                            current_url,
                        )
                        continue
                    try:
                        abs_url = urljoin(current_url, href)
                        parsed = urlparse(abs_url)
                    except ValueError:
                        # Unparseable href (bad port, invalid IPv6 literal, …)
                        continue

                    if parsed.scheme not in ("http", "https"):
                        continue
                    if parsed.netloc.lower() != base_domain:
                        continue
                    # Defense in depth: a path segment that is a bare scheme
                    # token ("https;", "http:") proves the URL was built from
                    # scheme-like garbage — never worth crawling.
                    if any(
                        _MALFORMED_PATH_SEGMENT_RE.match(seg)
                        for seg in parsed.path.split("/")
                        if seg
                    ):
                        continue
                    # Skip URLs with consecutive identical path segments — these
                    # are typically produced when a relative href re-states a
                    # segment that is already the last component of the current
                    # page URL (e.g. page at /.github/ + href ".github/LABELS.md"
                    # → /.github/.github/LABELS.md).  Such URLs almost always
                    # 404, so this protective heuristic skips them; doubled
                    # pairs that are part of the source's own base URL are
                    # exempt.
                    if _has_doubled_path_segment(
                        parsed.path, allowed=base_doubled_segments
                    ):
                        logger.debug(
                            "Website sync: skipping URL with doubled path segment"
                            " %r on %s",
                            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                            current_url,
                        )
                        continue

                    link_path = parsed.path.rstrip("/").lower()

                    if base_path and not link_path.startswith(base_path):
                        continue

                    path_segments = {s for s in link_path.split("/") if s}
                    if path_segments & _SKIP_PATH_SEGMENTS:
                        continue

                    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    if clean_url.rstrip("/") in visited:
                        continue
                    if any(parsed.path.lower().endswith(ext) for ext in skip_exts):
                        continue

                    bfs_queue.append((clean_url, depth + 1))

    # Guard: if the crawl produced no documents and at least one fetch failed
    # with a permanent HTTP error (404, 403, 401, malformed URL, …), the
    # source URL is likely broken or no longer exists.  Raise an exception so
    # the run is recorded as a failure — without this guard the scheduler
    # treats the run as a zero-document *success*, resets sync_perm_fail_count
    # to 0, and the source retries indefinitely.
    #
    # Two flavours, matching the scheduler's _is_permanent_sync_error
    # classification contract:
    #   * no transient signal in the whole run (no transient failures, no
    #     throttled recoveries) → the message embeds
    #     WEBSITE_ALL_PERMANENT_FAILURES_MARKER so _handle_sync_failure
    #     increments the strict sync_perm_fail_count budget and auto-disables
    #     the source after N consecutive runs;
    #   * any transient signal → a plain failure message (no permanent
    #     marker), so the run still fails (backoff retry, failure alerting)
    #     but an outage-/rate-limit-tainted run can never creep the perm-fail
    #     budget toward quarantine.
    #
    # The stale-cleanup pass is intentionally skipped on both paths: we must
    # not delete previously-synced content just because the remote URLs are
    # currently (or permanently) broken.
    if not all_doc_ids and total_permanent_failures > 0:
        if not transient_signal_observed:
            raise ValueError(
                f"Website source returned no content and "
                f"{WEBSITE_ALL_PERMANENT_FAILURES_MARKER} "
                f"({total_permanent_failures} URL(s) hit 404/403/401, a "
                "malformed URL, or a redirect loop) — the configured URL "
                "appears to be broken or gone. Update the source URL and "
                "use Force Re-sync to retry."
            )
        raise ValueError(
            "Website source returned no content: "
            f"{total_permanent_failures + total_transient_failures} URL "
            "fetch(es) failed and transient errors (timeouts, rate limits) "
            "were observed during the crawl. The site may be temporarily "
            "unavailable — previously synced content is preserved and the "
            "sync will be retried."
        )

    from app.core.database import async_session as _async_session

    async with _async_session() as cleanup_db:
        # The stale-cleanup DELETE runs in its own fresh session too, so it
        # needs the elevated sync timeout for bulk deletes on large KBs.
        await _apply_sync_statement_timeout(cleanup_db, statement_timeout_secs)
        kb_ref = await cleanup_db.get(KnowledgeBase, kb.id)
        if kb_ref:
            deleted = await _delete_stale_documents(
                cleanup_db,
                kb_ref,
                "website",
                all_doc_ids,
                sync_source_id=sync_source_id,
            )
            # Each changed page moved the revision as it committed; this
            # transaction only deletes.
            await _recount_kb_stats(cleanup_db, kb_ref, content_changed=bool(deleted))
            await cleanup_db.commit()
        else:
            deleted = 0
    await enqueue_documents(embed_doc_ids)

    if deleted:
        logger.info(
            "Website sync: removed %d stale page(s) from KB '%s'", deleted, kb.name
        )
    logger.info(
        "Website sync for KB '%s': crawled %d pages, %d docs total, %d new/updated from %s",
        kb.name,
        len(visited),
        len(all_doc_ids),
        len(embed_doc_ids),
        url,
    )
    return list(all_doc_ids)
