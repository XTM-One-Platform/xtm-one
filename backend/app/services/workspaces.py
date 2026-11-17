# SPDX-License-Identifier: Apache-2.0

"""Workspace creation helpers — slugs, and the one-default-per-user invariant.

Two things live here rather than in the router because both are about
*concurrency*, and a router handler is the wrong place to reason about it:
a slug must be unique per owner, and every owner must end up with exactly one
default workspace even when two of their requests race on different replicas.

Both are backed by partial unique indexes (see ``app.models.workspace``). The
database is the authority; the functions below are the retry that turns a
lost race into the winner's row instead of a 500.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fleet import Fleet
from app.models.workspace import (
    WORKSPACE_DEFAULT_NAME,
    WORKSPACE_DEFAULT_SLUG,
    Workspace,
    WorkspaceKnowledgeBase,
)

logger = logging.getLogger(__name__)

#: Matches the column width in the model.
SLUG_MAX_LENGTH = 200

#: How many suffixed candidates to try before giving up on a readable slug.
#: Past this a name is being collided with deliberately and a random tail is
#: a better answer than a longer scan.
_MAX_SLUG_ATTEMPTS = 50

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

logger = logging.getLogger(__name__)


def _defaults_of(user_id: uuid.UUID) -> Select[tuple[Workspace]]:
    """The query for *user_id*'s default workspaces, oldest first.

    ``uq_workspaces_default_per_user`` allows at most one row, but a data fix,
    a migration bug or raw SQL can break that, and a reader that insists on
    exactly one (``scalar_one_or_none``) then raises ``MultipleResultsFound``:
    a 500 on every listing, with no UI left to repair it. So every
    reader goes through this query and none assumes the count. The order makes
    the pick deterministic, so repeated reads land on the same workspace.
    """
    return (
        select(Workspace)
        .where(
            Workspace.user_id == user_id,
            Workspace.is_default.is_(True),
        )
        .order_by(Workspace.created_at.asc(), Workspace.id.asc())
    )


def slugify(name: str) -> str:
    """A URL-safe handle derived from a display name.

    Never returns empty: a name made entirely of characters this drops (an
    emoji, a CJK title) would otherwise produce a blank slug that collides
    with every other such name, so it falls back to a stable placeholder and
    lets :func:`unique_slug` disambiguate it.
    """
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")
    return (slug or "workspace")[:SLUG_MAX_LENGTH]


async def unique_slug(
    db: AsyncSession, user_id: uuid.UUID | None, base: str
) -> str:
    """A slug free for this owner, suffixed if the base is taken.

    Advisory, not a guarantee: this reads, and a concurrent insert can take
    the slug between the read and the write. The partial unique index is what
    actually enforces it, and the caller retries — see
    :func:`create_workspace`.
    """
    base = slugify(base)
    taken = set(
        (
            await db.execute(
                select(Workspace.slug).where(Workspace.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    for n in range(2, _MAX_SLUG_ATTEMPTS + 2):
        suffix = f"-{n}"
        candidate = base[: SLUG_MAX_LENGTH - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    tail = f"-{uuid.uuid4().hex[:8]}"
    return base[: SLUG_MAX_LENGTH - len(tail)] + tail


async def create_workspace(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    name: str,
    slug: str | None = None,
    **fields,
) -> Workspace:
    """Insert a workspace, retrying once if its slug was taken mid-flight.

    The retry exists because :func:`unique_slug` reads before it writes. One
    retry is enough: it recomputes against the state that caused the
    collision, so a second failure would mean a sustained collision storm on
    one owner's slugs rather than an ordinary race.
    """
    # Flush the caller's own pending state FIRST, outside the try: an
    # IntegrityError raised by their rows is not our slug race, and retrying
    # on it would both hide their bug and re-run against a poisoned
    # transaction.
    await db.flush()
    for attempt in (1, 2):
        candidate = slug if attempt == 1 and slug else None
        candidate = candidate or await unique_slug(db, user_id, name)
        workspace = Workspace(
            user_id=user_id, name=name, slug=candidate, **fields
        )
        try:
            async with db.begin_nested():
                db.add(workspace)
                await db.flush()
            return workspace
        except IntegrityError:
            if attempt == 2:
                raise
            # The savepoint rolled the failed insert back; the caller's
            # transaction is intact, so recompute and try once more.
            slug = None
    raise AssertionError("unreachable")  # pragma: no cover


async def ensure_default_workspace(db: AsyncSession, user_id: uuid.UUID) -> Workspace:
    """Return this user's default workspace, creating it on first use.

    TWO partial uniques can reject this insert, and conflating them was a bug.

    ``uq_workspaces_default_per_user`` is the race: two concurrent first-use
    requests both find nothing and both insert, exactly one wins, and the
    loser's IntegrityError is absorbed by the SAVEPOINT and answered by
    re-reading the winner's row. Without the savepoint the failed insert would
    poison the caller's transaction, so the "loser" would 500 on a request
    that did nothing wrong.

    ``uq_workspaces_slug_per_user`` is NOT a race and has nothing to do with
    concurrency: a user who created a workspace named "General" before ever
    listing owns the slug ``general``, and still has no default. The first
    version treated every IntegrityError as the race, found no winner to
    re-read, and raised — so ``GET /workspaces`` 500ed for that user, on every
    call, for ever. Since the Workspaces surface lists on mount, they could
    not even reach the UI that would let them rename the colliding workspace.

    So a slug collision is retried once with a disambiguated slug — the same
    ``unique_slug`` disambiguation ``create_workspace`` already does, which is
    where this should have gone from the start. The default's slug is a handle,
    not an identity: nothing persisted references it, and a user whose default
    is ``general-2`` has lost nothing.

    Deliberately does NOT commit: it is called from within a request's
    transaction and must be atomic with whatever prompted it.
    """

    async def _read_default() -> Workspace | None:
        return (await db.execute(_defaults_of(user_id).limit(1))).scalar_one_or_none()

    async def _try_insert(slug: str) -> Workspace | None:
        """Insert the default under *slug*, or None when a unique rejected it.

        The SAVEPOINT is what makes a rejection survivable: it rolls back the
        failed insert alone, leaving the caller's transaction intact.
        """
        candidate = Workspace(
            user_id=user_id,
            name=WORKSPACE_DEFAULT_NAME,
            slug=slug,
            is_default=True,
        )
        try:
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()
        except IntegrityError:
            return None
        return candidate

    existing = await _read_default()
    if existing is not None:
        return existing

    # Same reasoning as `create_workspace`: the caller's pending state is
    # flushed outside the try, so only our own insert can be the failure this
    # absorbs.
    await db.flush()

    created = await _try_insert(WORKSPACE_DEFAULT_SLUG)
    if created is not None:
        return created

    # Which unique rejected it? A winner means the default-per-user race, and
    # the winner's row is the answer.
    winner = await _read_default()
    if winner is not None:
        return winner

    # No winner, so the DEFAULT is still free and the SLUG was what collided.
    # Retry once under a free one.
    created = await _try_insert(await unique_slug(db, user_id, WORKSPACE_DEFAULT_NAME))
    if created is not None:
        return created

    # Both inserts rejected and still no default: someone created theirs
    # between the re-read and the retry. That is the race again, one lap later.
    winner = await _read_default()
    if winner is None:
        # Not the race, not the slug. Surface it rather than returning None
        # into a caller that has no branch for it.
        raise RuntimeError(
            f"Could not create or find a default workspace for user {user_id}"
        )
    return winner


async def unfile_workspace_conversations(db: AsyncSession, workspace: Workspace) -> int:
    """Take a doomed workspace's conversations out of it. Returns how many.

    They become unfiled - in no workspace at all - never moved to a default.
    A conversation outside every workspace is a normal state, the one every
    conversation starts in, and the people who filed a thread in the deleted
    workspace did not choose their default for it; nor can the deleter choose
    for a colleague whose threads sat in a shared workspace.

    The FK is ``ON DELETE SET NULL`` and would do the same; this runs it
    explicitly, in the delete's transaction, so the count reaches the audit
    entry and the assistant's answer.
    """
    from sqlalchemy import update

    from app.models.chat import Conversation

    result = await db.execute(
        update(Conversation)
        .where(Conversation.workspace_id == workspace.id)
        .values(workspace_id=None)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


# ── Scope resolution ─────────────────────────────────────────────────


class WorkspaceScopeError(Exception):
    """A workspace-scoped capability could not be resolved.

    Carries a machine-readable *reason* so the fail-closed path is observable
    (the flow runner logs it with the run and node) rather than only legible
    in a message.
    """

    #: `missing` (no workspace named), `not_found` (deleted, never existed, or
    #: not visible — deliberately one reason, see `resolve_workspace_scope`),
    #: `archived`, `unknown_scope` (a scope value outside the vocabulary).
    REASONS = ("missing", "not_found", "archived", "unknown_scope")

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def workspace_namespace(workspace_id: uuid.UUID | str) -> str:
    """The key namespace for a workspace-scoped coordination capability.

    Built on the **id**, never the slug: the slug is the human handle and a
    rename must not orphan existing state.

    This exists ALONGSIDE the `workspace_id` column rather than instead of it.
    The column gives referential integrity and a delete behaviour in DDL; the
    namespace means a query that forgets to filter on the column MISSES
    instead of crossing the boundary. Isolation should not depend on every
    call site remembering a filter — and the read path is the
    security-relevant part of this feature.
    """
    return f"workspace:{workspace_id}"


async def resolve_workspace_scope(
    db: AsyncSession,
    user,
    workspace_ref: str | uuid.UUID | None,
    *,
    user_group_ids: list[str] | None = None,
) -> Workspace:
    """Resolve a workspace-scoped capability, or raise.

    **Fails closed, always.** Every failure raises rather than returning None,
    because the only alternative a caller could fall back to is the
    un-namespaced global namespace — and collapsing an isolated namespace into
    the shared one is a data-leak shape, not a degradation. That is why this
    returns a `Workspace` and never `Workspace | None`: there is no shape in
    which a caller can accidentally proceed without one.

    Archived counts as a failure. Archiving means "hidden from every picker,
    contents kept", so a flow pointed at an archived workspace is repaired by
    restoring it — a loud refusal at resolution time is a better error than
    letting a writing agent fail mid-turn.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    if not workspace_ref:
        raise WorkspaceScopeError(
            "missing",
            "This capability is scoped to a workspace but none is set. "
            "Pick a workspace on the node, or file the conversation into one.",
        )
    try:
        workspace_id = (
            workspace_ref
            if isinstance(workspace_ref, uuid.UUID)
            else uuid.UUID(str(workspace_ref))
        )
    except (ValueError, AttributeError, TypeError):
        raise WorkspaceScopeError(
            "not_found", f"'{workspace_ref}' is not a valid workspace id."
        ) from None

    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)

    workspace = (
        await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                user_visible_filter(
                    Workspace, user.id, user_group_ids, is_admin=user.is_admin
                ),
            )
        )
    ).scalar_one_or_none()

    if workspace is None:
        # Deleted and not-visible are answered with ONE reason on purpose:
        # distinguishing them would tell a caller that a workspace they cannot
        # see exists, which is the existence oracle the visibility filter is
        # there to prevent.
        raise WorkspaceScopeError(
            "not_found",
            "That workspace does not exist, or is not shared with you.",
        )
    if workspace.is_archived:
        raise WorkspaceScopeError(
            "archived",
            f"Workspace '{workspace.name}' is archived. Restore it before "
            "using it as a scope.",
        )
    return workspace


async def count_workspace_work_items(db: AsyncSession, workspace: Workspace) -> int:
    """How many work items would block deleting this workspace.

    All of them, not only the open ones: the foreign key is RESTRICT, so a
    closed work item blocks the delete exactly as a live one does.
    """
    from sqlalchemy import func

    from app.models.work_item import WorkItem

    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(WorkItem)
                .where(WorkItem.workspace_id == workspace.id)
            )
        ).scalar()
        or 0
    )


async def purge_workspace_worksets(db: AsyncSession, workspace: Workspace) -> int:
    """Purge the workspace's worksets THROUGH `purge_workset`. Returns the count.

    This function exists because the FK is ``RESTRICT`` and must stay that way.
    A ``CASCADE`` would delete each `worksets` row without ever calling
    :func:`app.services.workset.purge_workset` — the only code that knows what
    a workset owns — orphaning its ``KnowledgeBase``, every
    ``KnowledgeDocument`` it owns and every ``Embedding``. That is the storage
    leak the retention sweep exists to prevent, and it would be undetectable
    afterwards, because the row that knew what those belonged to is the one
    that got cascaded away.

    So the ordering is the contract: purge each workset, THEN delete the
    workspace. ``RESTRICT`` is what makes skipping the first step fail loudly
    instead of leaking silently. Every member's worksets in the workspace go,
    not only the deleter's: they belong to the workspace being deleted.

    `purge_workset` is deliberately bounded and stays so: it deletes only the
    documents a workset OWNS (`document_is_foreign IS FALSE`), never a
    borrowed one — a workspace must not take content out of somebody else's
    knowledge base on its way out.
    """
    from app.models.workset import Workset
    from app.services.workset import purge_workset

    worksets = (
        (
            await db.execute(
                select(Workset).where(Workset.workspace_id == workspace.id)
            )
        )
        .scalars()
        .all()
    )
    for workset in worksets:
        await purge_workset(db, workset)
    return len(worksets)


# ── Which workspaces a caller may read from, and write to (OQ5) ───────
#
# These are the security-relevant half of the workspace scope, and the
# asymmetry between them IS the decision:
#
#   READ  follows the workspace's three-tier visibility, so anyone who can see
#         a workspace sees the state agents built in it. That is what makes it
#         a shared container rather than a private one.
#   WRITE requires the `config` tier — owner, admin, or a `config` /
#         `config_with_chats` grantee. A `read_only` grantee who could write
#         would be injecting state that OTHER agents then act on: they gain no
#         permission on paper, they simply steer the agents of everyone who can
#         see the workspace. That is a privilege escalation shaped like a
#         feature.
#
# No new tier is invented — `can_manage` already draws exactly this line, and
# reusing it is what keeps the two from drifting.


async def _visible_workspaces(
    db: AsyncSession, user
) -> tuple[list[Workspace], list[str]]:
    """Every workspace visible to *user*, plus their group ids.

    The group ids ride out with the rows because `can_manage` needs them and
    fetching them twice for one decision is the kind of waste that turns into
    a per-row query later.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    group_ids = await get_user_group_ids(db, user.id)
    rows = (
        (
            await db.execute(
                select(Workspace).where(
                    user_visible_filter(
                        Workspace, user.id, group_ids, is_admin=user.is_admin
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows), group_ids


async def workspace_scope_ids(
    db: AsyncSession, user, *, writable: bool
) -> list[uuid.UUID]:
    """Ids of the workspaces this caller may read state from, or write to.

    Returns a LIST, meant for an ``IN`` clause. Empty means "no workspace
    tier" — and an empty ``IN`` renders as false in SQLAlchemy, which is the
    correct fail-closed shape: a caller with no workspaces sees no
    workspace-scoped rows rather than all of them.

    Archived workspaces are excluded from BOTH sets. `resolve_workspace_scope`
    refuses to resolve one, so state can no longer be written there; leaving it
    readable would mean a picker that hides a workspace while its state still
    surfaced in a list, which is worse than a clean cut.
    """
    readable, writable_ids = await _scope_tiers_for(db, user)
    return writable_ids if writable else readable


async def _scope_tiers_for(
    db: AsyncSession, user
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    from app.api.v1.helpers import can_manage

    workspaces, group_ids = await _visible_workspaces(db, user)
    live = [w for w in workspaces if not w.is_archived]
    return (
        [w.id for w in live],
        [w.id for w in live if can_manage(user, w, group_ids)],
    )


async def workspace_scope_tiers(
    db: AsyncSession, user_id: str | None
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """``(readable, writable)`` workspace ids for a coordination caller.

    One pass for both: the visible workspaces are loaded once and the write
    tier is derived from them with ``can_manage``. Both lists are empty for an
    anonymous or unknown caller, the fail-closed shape (an empty ``IN``
    renders false).
    """
    user = await load_user_for_scope(db, user_id)
    if user is None:
        return [], []
    return await _scope_tiers_for(db, user)


async def load_user_for_scope(db: AsyncSession, user_id: str | None):
    """The `User` behind a tool's ``user_id`` string, or None.

    The coordination services take a user id rather than a user, because they
    open their own sessions. Both workspace tiers need `is_admin` and the
    caller's groups, so the row has to be loaded — once per operation, at the
    top, rather than per row.
    """
    from app.core.utils import safe_uuid
    from app.models.user import User

    uid = safe_uuid(str(user_id)) if user_id else None
    return await db.get(User, uid) if uid is not None else None


async def load_workspace_contents(
    db: AsyncSession,
    workspaces: Sequence[Workspace],
    *,
    user_id: str | uuid.UUID | None = None,
) -> dict[str, dict[str, int]]:
    """What each workspace holds, keyed by workspace id.

    One query per SCOPED THING, never one per workspace: a management view
    lists every workspace a person can see, and a count fetched per row is the
    N+1 that turns a tidy page into a slow one. Five queries answer for any
    number of workspaces.

    Returns a dict per workspace with every key present and zero where a
    workspace holds none of that thing — so a caller can render a row without
    checking for missing keys. "Nobody counted" is expressed by not calling
    this at all, which is why the response field is optional rather than
    defaulted.

    The work items, shared-state entries and worksets are the workspace's
    POPULATION, the one its Operations tab lists
    (:func:`app.services.fleet_views.workspace_scope_clause`): what is filed
    in it, plus what a current home member of a fleet it hosts owns (a work
    item: or created) and *user_id* can see. ``held_work_items`` stays the
    items FILED in it, the ones that block a delete (RESTRICT). Without
    *user_id* only what is filed is counted.
    """
    from sqlalchemy import func

    from app.models.chat import Conversation
    from app.models.shared_state import SharedState
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem
    from app.models.workset import Workset

    ids = [w.id for w in workspaces]
    empty = {
        "conversations": 0,
        "work_items": 0,
        "held_work_items": 0,
        "open_work_items": 0,
        "shared_states": 0,
        "worksets": 0,
        "knowledge_bases": 0,
    }
    counts: dict[str, dict[str, int]] = {str(i): dict(empty) for i in ids}
    if not ids:
        return counts

    async def _tally(model, key: str, *extra) -> None:
        rows = (
            await db.execute(
                select(model.workspace_id, func.count())
                .where(model.workspace_id.in_(ids), *extra)
                .group_by(model.workspace_id)
            )
        ).all()
        for workspace_id, total in rows:
            counts[str(workspace_id)][key] = int(total or 0)

    await _tally(Conversation, "conversations")
    await _tally(WorkItem, "work_items")
    # The total blocks a delete (RESTRICT); the live ones are every
    # non-terminal status, not only `open` - picked-up work is live work.
    await _tally(
        WorkItem, "open_work_items", WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES)
    )
    await _tally(SharedState, "shared_states")
    await _tally(Workset, "worksets")
    await _tally(WorkspaceKnowledgeBase, "knowledge_bases")
    for c in counts.values():
        c["held_work_items"] = c["work_items"]
    if user_id is not None:
        await _add_fleet_produced(db, ids, counts, user_id)
    return counts


async def _add_fleet_produced(
    db: AsyncSession,
    workspace_ids: list[uuid.UUID],
    counts: dict[str, dict[str, int]],
    user_id: str | uuid.UUID,
) -> None:
    """Add to *counts* what the hosted fleets' home members produced elsewhere.

    One grouped query per table for every workspace (never one per row): a
    row not filed in the hosting workspace, owned (a work item: or created)
    by a current home member of a fleet it hosts, and visible to the caller
    under the same rule as its Operations list. ``COUNT(DISTINCT id)``: an
    item a member owns and another created counts once.
    """
    from sqlalchemy import or_

    from app.core.utils import safe_uuid
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember
    from app.models.shared_state import SharedState
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem
    from app.models.workset import Workset
    from app.services.shared_state import shared_state_visible_clause
    from app.services.work_item import work_item_visible_clause

    uid = user_id if isinstance(user_id, uuid.UUID) else safe_uuid(str(user_id))
    if uid is None:
        return
    readable, _ = await workspace_scope_tiers(db, str(uid))
    hosted = (
        select(
            Fleet.workspace_id.label("workspace_id"),
            FleetMember.agent_id.label("agent_id"),
        )
        .join(FleetMember, FleetMember.fleet_id == Fleet.id)
        .where(
            Fleet.workspace_id.in_(workspace_ids),
            FleetMember.role == FLEET_ROLE_MEMBER,
            FleetMember.agent_id.isnot(None),
        )
        .subquery()
    )

    async def _extra(model, agent_match, visible, *keys_and_filters) -> None:
        columns = [hosted.c.workspace_id]
        for _key, extra in keys_and_filters:
            count = func.count(func.distinct(model.id))
            columns.append(count.filter(extra) if extra is not None else count)
        rows = (
            await db.execute(
                select(*columns)
                .select_from(model)
                .join(hosted, agent_match)
                .where(
                    visible,
                    or_(model.workspace_id.is_(None), model.workspace_id != hosted.c.workspace_id),
                )
                .group_by(hosted.c.workspace_id)
            )
        ).all()
        for row in rows:
            bucket = counts.get(str(row[0]))
            if bucket is None:
                continue
            for (key, _extra_filter), value in zip(keys_and_filters, row[1:], strict=True):
                bucket[key] += int(value or 0)

    await _extra(
        WorkItem,
        or_(
            WorkItem.owner_agent_id == hosted.c.agent_id,
            WorkItem.created_by_agent_id == hosted.c.agent_id,
        ),
        work_item_visible_clause(uid, readable),
        ("work_items", None),
        ("open_work_items", WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES)),
    )
    await _extra(
        SharedState,
        SharedState.owner_agent_id == hosted.c.agent_id,
        shared_state_visible_clause(uid, readable),
        ("shared_states", None),
    )
    await _extra(
        Workset,
        Workset.owner_agent_id == hosted.c.agent_id,
        or_(Workset.user_id == uid, Workset.is_company_managed.is_(True)),
        ("worksets", None),
    )



# ── One set of write rules for every surface ─────────────────────────
#
# The REST routes and the agent-builder workspace tools (a person asking their
# assistant to create a workspace, file a thread, bind a corpus) make the SAME
# decisions: the default can be neither archived nor deleted, a workspace
# holding work items is not deleted, an archived one takes no conversation, a
# corpus the caller cannot see is never bound. Each surface words the refusal
# its own way - a status code, a sentence for the model - so the rules live
# here and raise :class:`WorkspaceActionRefusedError`, and neither surface
# re-spells them.

#: Refusal wording shared by every surface.
DEFAULT_ARCHIVE_REFUSED = (
    "The default workspace cannot be archived. Make another workspace the "
    "default first."
)
DEFAULT_DELETE_REFUSED = (
    "The default workspace cannot be deleted. Make another workspace the "
    "default first."
)
ARCHIVED_FILING_REFUSED = "That workspace is archived."
FOREIGN_DEFAULT_REFUSED = "Only a workspace you own can be your default."
ARCHIVED_DEFAULT_REFUSED = "An archived workspace cannot be the default."
DEFAULT_COMPANY_MANAGED_REFUSED = (
    "The default workspace is personal and cannot be company-managed. Make "
    "another workspace the default first."
)
COMPANY_MANAGED_DEFAULT_REFUSED = "A company-managed workspace cannot be the default."


class WorkspaceActionRefusedError(Exception):
    """A workspace write the product refuses, with the status REST answers it with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def hosted_fleets_refusal(count: int) -> str:
    """Why a workspace still hosting *count* fleets is not deleted.

    The workspaces menu shows the same words before the click."""
    if count == 1:
        return (
            "This workspace hosts 1 fleet: it is its coordination. "
            "Move it to another workspace or delete it first."
        )
    return (
        f"This workspace hosts {count} fleets: it is their coordination. "
        "Move them to another workspace or delete them first."
    )


def hosted_fleets_archive_refusal(count: int) -> str:
    """Why a workspace still hosting *count* live fleets is not archived.

    An archived workspace takes nothing new: a live fleet left in one would
    have its agents coordinate where nobody can file. Archived fleets do not
    count - they coordinate nothing any more.
    """
    if count == 1:
        return "This workspace hosts 1 fleet: archive or move it first."
    return f"This workspace hosts {count} fleets: archive or move them first."


@dataclass(frozen=True)
class HostedFleet:
    """A fleet a workspace hosts, as the workspace surfaces name it."""

    id: uuid.UUID
    name: str
    slug: str
    is_archived: bool


@dataclass(frozen=True)
class HostedFleets:
    """The fleets each workspace hosts: the counts every caller may know (they
    decide whether deleting or archiving is possible), and the fleets the
    caller can SEE, the only ones named."""

    total: int = 0
    live: int = 0
    visible: tuple[HostedFleet, ...] = ()


async def hosted_fleets_of(
    db: AsyncSession,
    workspace_ids: Sequence[uuid.UUID],
    visible_filter=None,
) -> dict[uuid.UUID, HostedFleets]:
    """The fleets hosted by each of *workspace_ids*, in one query.

    *visible_filter* is the caller's visibility clause on :class:`Fleet`; a
    fleet outside it is counted, never named, like a bound knowledge base the
    reader cannot see. ``None`` names every fleet (an internal caller).
    """
    ids = list(dict.fromkeys(i for i in workspace_ids if i is not None))
    if not ids:
        return {}
    visible_column = (
        visible_filter.label("visible") if visible_filter is not None else None
    )
    columns = [Fleet.workspace_id, Fleet.id, Fleet.name, Fleet.slug, Fleet.is_archived]
    if visible_column is not None:
        columns.append(visible_column)
    rows = (
        await db.execute(
            select(*columns).where(Fleet.workspace_id.in_(ids)).order_by(Fleet.name, Fleet.id)
        )
    ).all()
    grouped: dict[uuid.UUID, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    hosted: dict[uuid.UUID, HostedFleets] = {}
    for workspace_id, fleet_rows in grouped.items():
        hosted[workspace_id] = HostedFleets(
            total=len(fleet_rows),
            live=sum(1 for r in fleet_rows if not r[4]),
            visible=tuple(
                HostedFleet(id=r[1], name=r[2], slug=r[3], is_archived=bool(r[4]))
                for r in fleet_rows
                if visible_column is None or bool(r[5])
            ),
        )
    return hosted


def held_work_items_refusal(count: int) -> str:
    """Why a workspace still holding *count* work items is not deleted."""
    return (
        f"This workspace still holds {count} work "
        f"item{'s' if count != 1 else ''}. Deleting it would "
        "delete tracked work, so it is refused — archive the "
        "workspace instead, or clear its work items first."
    )


async def lock_workspace_for_hosting(db: AsyncSession, workspace: Workspace) -> Workspace:
    """Lock *workspace*'s row and re-read what decides whether it may host a fleet.

    Archiving a workspace and binding a fleet to it both take this lock
    first, so one waits for the other: an archive never misses a fleet
    bound concurrently, and a bind never accepts a workspace archived after
    the caller loaded it.
    """
    await db.execute(
        select(Workspace.id).where(Workspace.id == workspace.id).with_for_update()
    )
    await db.refresh(workspace, attribute_names=["is_archived", "is_company_managed"])
    return workspace


async def set_workspace_archived(db: AsyncSession, workspace: Workspace, archived: bool) -> None:
    """Archive or restore *workspace*, refusing to archive the default or a
    workspace that hosts a live fleet.

    Refused rather than silently promoting another workspace: which one
    should inherit "default" is the owner's call, not ours. A live fleet's
    workspace is its coordination: archived, it would take no new
    work while the fleet's agents keep coordinating in it.
    """
    if archived and workspace.is_default:
        raise WorkspaceActionRefusedError(400, DEFAULT_ARCHIVE_REFUSED)
    if archived and not workspace.is_archived:
        # Serialised with fleet binds on the workspace row: the count below
        # sees every fleet bound before the archive.
        await lock_workspace_for_hosting(db, workspace)
        live = (
            await db.execute(
                select(func.count(Fleet.id)).where(
                    Fleet.workspace_id == workspace.id, Fleet.is_archived.is_(False)
                )
            )
        ).scalar() or 0
        if live:
            raise WorkspaceActionRefusedError(409, hosted_fleets_archive_refusal(live))
    workspace.is_archived = archived


def ensure_can_be_company_managed(workspace: Workspace, company_managed: bool | None) -> None:
    """Refuse making a user's default workspace company-managed.

    The default is a per-owner property (``uq_workspaces_default_per_user``):
    it is where its owner lands. Company-managed, it would stay the owner's
    default while everyone saw it - with its `default` badge - and only admins
    could change it, the owner included. Sharing it with people or groups is
    fine: the owner keeps it.
    """
    if company_managed and not workspace.is_company_managed and workspace.is_default:
        raise WorkspaceActionRefusedError(400, DEFAULT_COMPANY_MANAGED_REFUSED)


def ensure_workspace_accepts_conversations(workspace: Workspace) -> None:
    """Refuse filing a conversation into an archived workspace.

    Archiving hides a workspace from every picker; filing a thread into one
    would put work somewhere nobody looks.
    """
    if workspace.is_archived:
        raise WorkspaceActionRefusedError(400, ARCHIVED_FILING_REFUSED)


async def make_default_workspace(
    db: AsyncSession, user_id: uuid.UUID, workspace: Workspace
) -> bool:
    """Make *workspace* the default of *user_id*. Returns whether it changed.

    Two statements, ordered: clear the current default first, then set the
    new one. The reverse order would transiently hold two defaults and trip
    ``uq_workspaces_default_per_user`` — the constraint doing exactly its job,
    on a request that is legitimate.

    The default is a per-user property keyed on the OWNER, so only a
    workspace the caller owns can be theirs; an archived one is refused, since
    the default is where a user lands and landing in something hidden from
    every picker is a dead end.
    """
    if workspace.user_id != user_id:
        raise WorkspaceActionRefusedError(400, FOREIGN_DEFAULT_REFUSED)
    if workspace.is_archived:
        raise WorkspaceActionRefusedError(400, ARCHIVED_DEFAULT_REFUSED)
    if workspace.is_company_managed:
        raise WorkspaceActionRefusedError(400, COMPANY_MANAGED_DEFAULT_REFUSED)
    if workspace.is_default:
        return False
    # Every current default is cleared, not just one: were the invariant ever
    # broken, clearing one would leave another default standing beside the new
    # one, and the user would not land where they chose. Clearing them all is
    # what repairs it.
    current = (await db.execute(_defaults_of(user_id))).scalars().all()
    if len(current) > 1:
        logger.error(
            "Invariant violation: user %s had %d default workspaces; clearing them all",
            user_id,
            len(current),
        )
    if current:
        for previous in current:
            previous.is_default = False
        await db.flush()
    workspace.is_default = True
    return True


@dataclass(frozen=True)
class WorkspaceDeletion:
    """What deleting a workspace did to what it held."""

    conversations_unfiled: int
    worksets_purged: int
    knowledge_documents_deleted: int = 0


async def remove_workspace(db: AsyncSession, workspace: Workspace) -> WorkspaceDeletion:
    """Delete *workspace* and settle what it holds. Does not commit.

    The default is refused: every user must keep exactly one. Five things can
    be attached to a workspace and each is treated differently, because each
    loses something different: conversations are unfiled (in no workspace,
    never moved to a default), bound knowledge bases and linked skills are
    untouched (only the link goes), the workspace's OWN knowledge bases go with
    it (:func:`purge_workspace_knowledge`), worksets are purged through
    ``purge_workset``, and the delete is
    REFUSED while any work item remains — ``work_items.workspace_id`` is
    ``RESTRICT`` and deleting a workspace must never delete tracked work, so
    the refusal carries the count instead of surfacing as a 500.
    """
    if workspace.is_default:
        raise WorkspaceActionRefusedError(400, DEFAULT_DELETE_REFUSED)
    # `fleets.workspace_id` is RESTRICT: a fleet is never left without its
    # coordination, and the refusal says why rather than surfacing a 500.
    hosted = (
        await db.execute(select(func.count(Fleet.id)).where(Fleet.workspace_id == workspace.id))
    ).scalar() or 0
    if hosted:
        raise WorkspaceActionRefusedError(409, hosted_fleets_refusal(hosted))
    held_items = await count_workspace_work_items(db, workspace)
    if held_items:
        raise WorkspaceActionRefusedError(409, held_work_items_refusal(held_items))
    # Worksets are RESTRICT too, but for the opposite reason: the row IS meant
    # to go, and only `purge_workset` knows what it owns.
    purged = await purge_workspace_worksets(db, workspace)
    unfiled = await unfile_workspace_conversations(db, workspace)
    documents = await purge_workspace_knowledge(db, workspace)
    await db.delete(workspace)
    return WorkspaceDeletion(
        conversations_unfiled=unfiled,
        worksets_purged=purged,
        knowledge_documents_deleted=documents,
    )


def _visible_linked(
    workspace: Workspace,
    link_model,
    link_column,
    target_model,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    *,
    is_admin: bool,
):
    """``SELECT`` the *target_model* rows linked to *workspace* that the caller
    can see - the one rule behind a workspace's bound knowledge bases and
    linked skills, listed or counted.

    A reader is only ever shown the targets THEY can see: a manager who links
    their private knowledge base or skill into a shared workspace does not
    publish its name to the workspace's readers.
    """
    from app.api.v1.helpers import user_visible_filter

    return (
        select(target_model)
        .join(link_model, link_column == target_model.id)
        .where(
            link_model.workspace_id == workspace.id,
            user_visible_filter(target_model, user_id, user_group_ids, is_admin=is_admin),
        )
    )


async def visible_bound_knowledge_bases(
    db: AsyncSession,
    workspace: Workspace,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    *,
    is_admin: bool,
):
    """The knowledge bases bound to *workspace* that the caller can see, by name."""
    from app.models.knowledge_base import KnowledgeBase

    query = _visible_linked(
        workspace,
        WorkspaceKnowledgeBase,
        WorkspaceKnowledgeBase.knowledge_base_id,
        KnowledgeBase,
        user_id,
        user_group_ids,
        is_admin=is_admin,
    )
    return list((await db.execute(query.order_by(KnowledgeBase.name.asc()))).scalars().all())


async def count_visible_links(
    db: AsyncSession,
    workspace: Workspace,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    *,
    is_admin: bool,
) -> tuple[int, int]:
    """``(bound knowledge bases, linked skills)`` the caller can see, counted
    in the database rather than loaded."""
    from sqlalchemy import func

    from app.models.agent_skill import AgentSkill
    from app.models.knowledge_base import KnowledgeBase
    from app.models.workspace import WorkspaceSkill

    counts = []
    for link_model, link_column, target_model in (
        (WorkspaceKnowledgeBase, WorkspaceKnowledgeBase.knowledge_base_id, KnowledgeBase),
        (WorkspaceSkill, WorkspaceSkill.skill_id, AgentSkill),
    ):
        query = _visible_linked(
            workspace, link_model, link_column, target_model, user_id, user_group_ids, is_admin=is_admin
        ).with_only_columns(func.count(target_model.id))
        counts.append(int((await db.execute(query)).scalar() or 0))
    return counts[0], counts[1]


async def rebind_workspace_knowledge_bases(
    db: AsyncSession,
    workspace: Workspace,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    *,
    is_admin: bool,
    wanted: set[uuid.UUID] | None = None,
    add: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
    remove: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Change the knowledge bases bound to *workspace*. Returns ``(added, removed)``.

    Either a replace (*wanted*: the complete set, what the UI sends back) or a
    delta (*add* / *remove*: what an assistant asked for), resolved in the
    same locked transaction, so a delta cannot undo a concurrent change the
    way a read-then-replace from the caller could.

    Every id ADDED is resolved through the caller's OWN visibility. Managing
    the workspace is not enough — otherwise binding would become a way to
    learn that a knowledge base exists, and to name it in a container
    somebody else can read. An id the caller cannot see is refused for that
    id, not silently dropped. The change covers the bindings the caller can
    see: a knowledge base another manager bound and this caller cannot see is
    kept, never unbound by a list that could never have named it.
    """
    from app.api.v1.helpers import bindable_knowledge_base_clause
    from app.models.knowledge_base import KnowledgeBase

    return await _relink(
        db,
        workspace,
        link_model=WorkspaceKnowledgeBase,
        link_column=WorkspaceKnowledgeBase.knowledge_base_id,
        target_model=KnowledgeBase,
        # A workspace's own knowledge base is never bound, here or anywhere
        # else.
        target_clause=bindable_knowledge_base_clause(),
        label="Knowledge base",
        user_id=user_id,
        user_group_ids=user_group_ids,
        is_admin=is_admin,
        wanted=wanted,
        add=add,
        remove=remove,
    )


async def _relink(
    db: AsyncSession,
    workspace: Workspace,
    *,
    link_model,
    link_column,
    target_model,
    label: str,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    is_admin: bool,
    wanted: set[uuid.UUID] | None = None,
    add: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
    remove: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
    target_clause=None,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Change what *workspace* links of *target_model*: the one rule behind its
    knowledge-base bindings and its skill links. Returns ``(added, removed)``.

    The workspace row is locked so two changes serialize (two concurrent
    requests adding the same target would otherwise both insert and the second
    trip the pair's unique index). Every target ADDED is resolved through the
    caller's own visibility - managing the workspace is not enough, or linking
    would reveal that a target exists - and one the caller cannot see is
    refused for that id. A link the caller cannot see is kept: a list that
    could never have named it does not unlink it.
    """
    from app.api.v1.helpers import user_visible_filter

    await db.execute(
        select(Workspace.id).where(Workspace.id == workspace.id).with_for_update()
    )
    current = {
        getattr(row, link_column.key): row
        for row in (
            (
                await db.execute(
                    select(link_model).where(link_model.workspace_id == workspace.id)
                )
            )
            .scalars()
            .all()
        )
    }
    if wanted is None:
        wanted = (set(current) | set(add)) - set(remove)
    candidates = wanted | set(current)
    clauses = [
        target_model.id.in_(candidates),
        user_visible_filter(target_model, user_id, user_group_ids, is_admin=is_admin),
    ]
    if target_clause is not None:
        clauses.append(target_clause)
    visible_ids = (
        set((await db.execute(select(target_model.id).where(*clauses))).scalars().all())
        if candidates
        else set()
    )
    added = wanted - set(current)
    missing = added - visible_ids
    if missing:
        raise WorkspaceActionRefusedError(
            404,
            f"{label} not found or not visible to you: "
            + ", ".join(sorted(str(m) for m in missing)),
        )
    removed = {target for target in current if target in visible_ids and target not in wanted}

    for target in removed:
        await db.delete(current[target])
    for target in added:
        db.add(link_model(workspace_id=workspace.id, **{link_column.key: target}))
    await db.flush()
    return added, removed


def workspace_reaches_others(workspace: Workspace, user_id: uuid.UUID) -> bool:
    """Whether people other than *user_id* can see *workspace*: someone else
    owns it, it is company-managed, or it is shared with a person or a group.
    Filing a conversation into such a workspace makes it searchable by them
    (spec 032 FR-022), so the person is told first."""
    others = {str(u) for u in workspace.shared_user_ids or []} - {str(user_id)}
    return bool(
        workspace.is_company_managed
        or workspace.shared_group_ids
        or others
        or (workspace.user_id is not None and workspace.user_id != user_id)
    )


# ── Workspace knowledge ──────────────────────────────────────────────
#
# A workspace owns at most one Sources and one Conversations knowledge base
# (``knowledge_bases.owner_workspace_id`` / ``workspace_role``) and links
# library skills (``workspace_skills``). What a turn receives from them is
# decided in ``services.workspace_knowledge``; these are the writes.

WORKSPACE_KB_SOURCES = "sources"
WORKSPACE_KB_CONVERSATIONS = "conversations"
_WORKSPACE_KB_LABELS = {
    WORKSPACE_KB_SOURCES: "Sources",
    WORKSPACE_KB_CONVERSATIONS: "Conversations",
}


async def workspace_kb(db: AsyncSession, workspace_id: uuid.UUID, role: str):
    """The workspace's knowledge base for *role*, or None when not created yet."""
    from app.models.knowledge_base import KnowledgeBase

    return (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.owner_workspace_id == workspace_id,
                KnowledgeBase.workspace_role == role,
            )
        )
    ).scalar_one_or_none()


async def ensure_workspace_kb(db: AsyncSession, workspace: Workspace, role: str):
    """The workspace's knowledge base for *role*, created on first use.

    The workspace row is locked first: two concurrent first writes would
    otherwise both see none and the loser would trip
    ``uq_knowledge_bases_workspace_role``. Neither the knowledge base's sharing
    columns nor its ``user_id`` are set: its visibility and its manage right
    are the workspace's (``KnowledgeBase.visibility_parent``,
    ``helpers.managing_workspace``), and an owner would be wrong twice - a
    workspace changes owner without its knowledge bases following, and
    ``knowledge_bases.user_id`` cascades, so deleting that person would take a
    company-managed workspace's sources while the workspace survives.

    The Conversations knowledge base is ``is_internal`` (the workset
    precedent): it holds what people wrote in their own threads, so no
    list, route or tool reaches it - only the workspace's search, which reads
    it by id and drops what is no longer searchable, and the indexer write it.
    """
    from app.core.utils import fit_copy_name
    from app.models.knowledge_base import KnowledgeBase

    if role not in _WORKSPACE_KB_LABELS:
        raise ValueError(f"Unknown workspace knowledge base role {role!r}")
    existing = await workspace_kb(db, workspace.id, role)
    if existing is not None:
        return existing
    await db.execute(
        select(Workspace.id).where(Workspace.id == workspace.id).with_for_update()
    )
    existing = await workspace_kb(db, workspace.id, role)
    if existing is not None:
        return existing

    try:
        from app.llm.router import get_embedding_provider

        _, embedding_model, _provider_id = await get_embedding_provider(db)
    except ValueError:
        embedding_model = "text-embedding-3-large"  # the KB creation's fallback
    label = _WORKSPACE_KB_LABELS[role]
    kb = KnowledgeBase(
        id=uuid.uuid4(),
        owner_workspace_id=workspace.id,
        workspace_role=role,
        name=fit_copy_name(workspace.name, KnowledgeBase.__table__.c.name, suffix=f" — {label}"),
        description=(
            f"{label} of a workspace. Managed from the workspace: it is shared, "
            "changed and deleted with it."
        ),
        embedding_model=embedding_model,
        is_internal=role == WORKSPACE_KB_CONVERSATIONS,
    )
    db.add(kb)
    await db.flush()
    return kb


async def visible_linked_skills(
    db: AsyncSession,
    workspace: Workspace,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    *,
    is_admin: bool,
):
    """The skills linked to *workspace* that the caller can see, by name."""
    from app.models.agent_skill import AgentSkill
    from app.models.workspace import WorkspaceSkill

    query = _visible_linked(
        workspace,
        WorkspaceSkill,
        WorkspaceSkill.skill_id,
        AgentSkill,
        user_id,
        user_group_ids,
        is_admin=is_admin,
    )
    return list((await db.execute(query.order_by(AgentSkill.name.asc()))).scalars().all())


async def relink_workspace_skills(
    db: AsyncSession,
    workspace: Workspace,
    user_id: uuid.UUID,
    user_group_ids: list[str],
    *,
    is_admin: bool,
    wanted: set[uuid.UUID],
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Replace the skills linked to *workspace* (:func:`_relink`'s rules).
    Returns ``(added, removed)``."""
    from app.models.agent_skill import AgentSkill
    from app.models.workspace import WorkspaceSkill

    return await _relink(
        db,
        workspace,
        link_model=WorkspaceSkill,
        link_column=WorkspaceSkill.skill_id,
        target_model=AgentSkill,
        label="Skill",
        user_id=user_id,
        user_group_ids=user_group_ids,
        is_admin=is_admin,
        wanted=wanted,
    )


async def purge_workspace_knowledge(db: AsyncSession, workspace: Workspace) -> int:
    """Settle what the database cascade cannot, before *workspace* is deleted.

    Returns how many documents the workspace's knowledge bases held. The rows
    themselves - knowledge bases, documents, embeddings, sync sources,
    activity, export packages - go with the workspace by ``ON DELETE CASCADE``.
    What a cascade cannot reach is outside the database: the queued processing
    jobs of those documents (cancelled once the delete commits, never under its
    lock) and the files of export packages in object storage.
    """
    from app.models.knowledge_base import KBExportPackage, KnowledgeBase, KnowledgeDocument
    from app.services.file_storage import file_storage
    from app.services.kb_helpers import cancel_document_jobs_after_commit

    owned = select(KnowledgeBase.id).where(KnowledgeBase.owner_workspace_id == workspace.id)
    doc_ids = (
        (
            await db.execute(
                select(KnowledgeDocument.id).where(KnowledgeDocument.knowledge_base_id.in_(owned))
            )
        )
        .scalars()
        .all()
    )
    cancel_document_jobs_after_commit(db, doc_ids)
    objects = (
        (
            await db.execute(
                select(KBExportPackage.s3_object_name).where(
                    KBExportPackage.knowledge_base_id.in_(owned)
                )
            )
        )
        .scalars()
        .all()
    )
    for object_name in objects:
        try:
            await file_storage.delete(object_name)
        except Exception as exc:
            # A leftover object is storage, not data anyone can reach: the row
            # pointing at it goes with the cascade. Never block the delete.
            logger.warning("Workspace export object %r was not deleted: %r", object_name, exc)
    return len(doc_ids)
