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

"""Sync ``.nov`` rules from a GitHub repo into company-managed NovaRules.

Row-driven, like ``KBSyncSource``: the scheduler picks due :class:`NovaRuleSource`
rows and calls :func:`sync_nova_rule_source`, which walks the repo's ``.nov``
files, splits multi-rule files, parses each rule, and upserts it as a
company-managed :class:`NovaRule` tagged with the source. Rules that were synced
before but are gone from the repo are pruned. Parsing uses the in-core keyword
parser here; the semantic/LLM tiers stay inert until the out-of-process engine
is deployed (Phase E), so a rule that relies only on those tiers imports and
sits idle rather than firing.

Public repos are fetched anonymously (the community rule set is public); the
GitHub tree API lists files, raw.githubusercontent.com serves content.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.nova_rule import NOVA_ACTION_LOG, NovaRule
from app.models.nova_rule_source import NovaRuleSource
from app.services import nova_guard

logger = logging.getLogger(__name__)

#: What a caller who cannot manage the Nova integration is served in place of a
#: source's ``last_sync_error``: the stored text is the exception of the
#: failed run - an HTTP client message with the request URL, a driver message -
#: kept for the people who fix the source.
READER_SYNC_ERROR_MESSAGE = (
    "The error details are visible only to people who can manage this integration."
)


def served_sync_error(stored: str | None, *, can_manage: bool) -> str | None:
    """The sync error a caller is served for a Nova rule source.

    A caller who can manage the integration (``helpers.can_manage``) is served
    the stored text, exactly as before; anyone else a fixed sentence. Nothing to
    hide is served as stored.
    """
    if can_manage or not stored:
        return stored
    return READER_SYNC_ERROR_MESSAGE

_GITHUB_API = "https://api.github.com"
_MAX_FILE_BYTES = 500_000
_MAX_BACKOFF_MINUTES = 360
_INTERVAL_MAP: dict[str, timedelta] = {
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}
_SYNC_TAG_PREFIX = "nova-source:"


def _sync_tag(source_id) -> str:
    """Tag stamped on every rule a source owns, so stale ones can be pruned."""
    return f"{_SYNC_TAG_PREFIX}{source_id}"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _list_nov_files(
    client: httpx.AsyncClient, repo: str, branch: str, path: str
) -> list[str]:
    url = f"{_GITHUB_API}/repos/{repo}/git/trees/{branch}?recursive=1"
    resp = await client.get(url)
    if resp.status_code == 409:  # empty repo
        return []
    resp.raise_for_status()
    tree = resp.json().get("tree", [])
    prefix = path.strip("/")
    files: list[str] = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        fpath = item.get("path") or ""
        if not fpath.endswith(".nov"):
            continue
        if prefix and not fpath.startswith(prefix):
            continue
        if item.get("size", 0) <= _MAX_FILE_BYTES:
            files.append(fpath)
    return files


async def _fetch_file(
    client: httpx.AsyncClient, repo: str, branch: str, fpath: str
) -> str | None:
    """Raw file content from the public repo, or ``None`` on a non-200."""
    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{fpath}"
    resp = await client.get(raw_url)
    if resp.status_code != 200:
        logger.info("Nova sync: skip %s (HTTP %s)", fpath, resp.status_code)
        return None
    return resp.text


async def sync_nova_rule_source(db: AsyncSession, source: NovaRuleSource) -> int:
    """Pull *source*'s ``.nov`` rules and upsert them. Returns the rule count.

    Raises on a hard transport failure (the scheduler records it and backs off);
    an individual unparseable rule is skipped, not fatal. A rule this source
    already owns whose new text is refused keeps its last version, and a file
    that could not be read means the snapshot is incomplete, so nothing is
    pruned. What was kept from before is written to ``source.last_sync_error``
    (``None`` when nothing was), the report of a sync that completed.
    """
    tag = _sync_tag(source.id)
    branch = source.branch or "main"

    existing = (
        (
            await db.execute(
                select(NovaRule).where(NovaRule.is_company_managed.is_(True))
            )
        )
        .scalars()
        .all()
    )
    by_name = {r.name: r for r in existing if tag in (r.tags or [])}
    # Company rules this source does NOT own — either hand-made by an admin or
    # owned by another source. Used to avoid creating a same-named duplicate.
    foreign_names = {r.name for r in existing if tag not in (r.tags or [])}
    seen: set[str] = set()
    declared: set[str] = set()
    unread = 0

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        files = await _list_nov_files(client, source.repo, branch, source.path or "")
        logger.info(
            "Nova sync: %d .nov files in %s@%s", len(files), source.repo, branch
        )
        for fpath in files:
            content = await _fetch_file(client, source.repo, branch, fpath)
            if content is None:
                unread += 1
                continue
            declared.update(nova_guard.declared_rule_names(content))
            for block in nova_guard.split_rules(content):
                try:
                    parsed = nova_guard.parse_rule(block)
                except nova_guard.NovaRuleError as exc:
                    logger.info("Nova sync: unparseable rule in %s — %s", fpath, exc)
                    continue
                if parsed.name in seen:
                    continue  # first definition wins across the repo
                seen.add(parsed.name)
                _upsert(db, by_name, foreign_names, tag, parsed, block)

    # A rule this source owns that the repo still declares but no longer
    # parses (refused, or a block that never closes) keeps its last version
    # and the admin's action: a bad upstream edit must not delete a rule
    # promoted to block.
    kept = sorted(declared & (by_name.keys() - seen))
    for name in kept:
        logger.warning("Nova sync: the update of rule %r is refused; keeping its last version", name)

    # Prune rules this source used to own but the repo no longer has - only
    # from a complete snapshot, and never a rule it kept.
    if not unread:
        for name, rule in by_name.items():
            if name not in seen and name not in kept:
                await db.delete(rule)
    source.last_sync_error = _sync_notice(kept, unread)
    if source.last_sync_error:
        logger.warning("Nova sync of %s: %s", source.repo, source.last_sync_error)

    from app.services.nova_engine import clear_pattern_embedding_cache

    clear_pattern_embedding_cache()
    # The rules the source holds after this sync: an incomplete snapshot removed none.
    return len(seen | set(kept) | (set(by_name) if unread else set()))


_SYNC_NOTICE_MAX_CHARS = 500
_SYNC_NOTICE_MAX_NAMES = 10


def _sync_notice(kept: list[str], unread: int) -> str | None:
    """What a sync that completed kept from before, for ``last_sync_error``."""
    parts: list[str] = []
    if kept:
        shown = ", ".join(kept[:_SYNC_NOTICE_MAX_NAMES])
        more = len(kept) - _SYNC_NOTICE_MAX_NAMES
        parts.append(
            f"Kept the last accepted version of {len(kept)} rule(s) the repository now has in a form "
            f"the platform refuses: {shown}{f' and {more} more' if more > 0 else ''}."
        )
    if unread:
        parts.append(f"{unread} rule file(s) could not be read, so no rule was removed.")
    notice = " ".join(parts)
    if len(notice) > _SYNC_NOTICE_MAX_CHARS:
        notice = notice[: _SYNC_NOTICE_MAX_CHARS - 3].rsplit(" ", 1)[0] + "..."
    return notice or None


def _upsert(db, by_name, foreign_names, tag: str, parsed, block: str) -> None:
    rule = by_name.get(parsed.name)
    if rule is None and parsed.name in foreign_names:
        # A company rule of this name already exists that this source does not
        # own. Do NOT insert a second one (both would evaluate, and a `block`
        # promotion on one would be invisible on the other), and do NOT adopt
        # it either: taking ownership of a hand-made rule would mean deleting
        # someone's work when Nova disconnects. Leave it alone and say so.
        logger.info(
            "Nova sync: rule %r already exists outside this source — skipping",
            parsed.name,
        )
        return
    if rule is not None:
        if tag not in (rule.tags or []):
            rule.tags = [*(rule.tags or []), tag]
        # Only the rule TEXT (and the category it carries) tracks the repo. An
        # admin's own decisions must survive a sync: `action` is not reset, so a
        # rule promoted to `block` stays blocking, and `enabled` is not touched,
        # so a rule an admin disabled is not silently switched back on.
        if rule.rule_text != block:
            rule.rule_text = block
            rule.category = parsed.category or rule.category
        return
    # New rule: default action is 'log' (audit) — an admin opts a rule up to
    # 'block' deliberately; a freshly-synced community rule never blocks silently.
    db.add(
        NovaRule(
            name=parsed.name,
            description="",
            category=parsed.category or "",
            action=NOVA_ACTION_LOG,
            rule_text=block,
            enabled=True,
            tags=[tag],
            is_company_managed=True,
        )
    )


async def process_scheduled_nova_syncs(db: AsyncSession) -> int:
    """Run every enabled NovaRuleSource that is due. Returns how many ran."""
    now = datetime.now(UTC)
    # Only sources owned by a company-managed Nova integration may run: they
    # write platform-wide (is_company_managed) rules, so a source left behind by
    # a personal connection must never sync. Defense in depth — the lifecycle
    # hooks already refuse to seed one.
    from app.models.integration import Integration

    sources = (
        (
            await db.execute(
                select(NovaRuleSource)
                .join(Integration, Integration.id == NovaRuleSource.integration_id)
                .where(
                    NovaRuleSource.enabled.is_(True),
                    # access_restricted: bypass - scheduled system sync of platform-wide rules, no acting user.
                    Integration.is_company_managed.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    ran = 0
    for source in sources:
        delta = _INTERVAL_MAP.get(source.sync_interval)
        if delta is None:  # "manual" or unknown → never auto-runs
            continue
        retry_at = _aware(source.sync_next_retry_at)
        if retry_at and retry_at > now:
            continue
        last = _aware(source.last_synced_at)
        if last and last + delta > now:
            continue
        await _run_one(db, source, now)
        ran += 1
    return ran


async def _run_one(db: AsyncSession, source: NovaRuleSource, now: datetime) -> None:
    try:
        count = await sync_nova_rule_source(db, source)
    except Exception as exc:
        logger.warning(
            "Nova sync failed for source %s (%s, integration %s): %s",
            source.id,
            source.repo,
            source.integration_id,
            format_exception_message(exc),
        )
        source.sync_failure_count = (source.sync_failure_count or 0) + 1
        backoff = min(2 ** source.sync_failure_count * 5, _MAX_BACKOFF_MINUTES)
        source.sync_next_retry_at = now + timedelta(minutes=backoff)
        source.last_sync_status = "error"
        source.last_sync_error = str(exc)[:500]
        await db.commit()
        return
    source.last_synced_at = now
    source.last_sync_status = "ok"
    # last_sync_error was written by the sync: what it kept from before, or None.
    source.last_sync_rules = count
    source.sync_failure_count = 0
    source.sync_next_retry_at = None
    await db.commit()
