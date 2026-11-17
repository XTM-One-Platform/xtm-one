# SPDX-License-Identifier: Apache-2.0

"""Workspace CRUD.

A Workspace is a named environment that will scope an owner's tools,
conversations and resources. This router is the entity's own surface: create,
list, read, update, archive, delete. The scoping itself — the ``workspace``
coordination scope, ``workspace_id`` on the tool tables and on conversations —
arrives in later increments and does not exist yet.

Two invariants are worth reading before changing anything here:

* **Every user has exactly one default workspace**, created lazily on first
  use. ``GET ""`` is what triggers it, so a caller never sees an empty list
  and no migration had to write a row per existing user.
* **The default cannot be archived or deleted.** Both would break the
  invariant above, and both are refused with a reason rather than silently
  promoting another workspace — which of the caller's other workspaces should
  inherit "default" is not a question this endpoint can answer for them.

Gating follows ``flows``: ``require_agentic_ai_licensed``, the licence
question only. Managing a workspace invokes no model, so it must keep working
on a deployment where AI is switched off — the same promise deterministic
flows make (FR-014).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai_licensed
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    can_manage,
    get_manageable_entity,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    parse_uuid_list,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.workspace import (
    WorkspaceContents,
    WorkspaceCreate,
    WorkspaceHostedFleet,
    WorkspaceKnowledgeBaseRef,
    WorkspaceKnowledgeBasesUpdate,
    WorkspaceResponse,
    WorkspaceUpdate,
)
from app.core.database import get_db
from app.core.utils import safe_uuid
from app.models.fleet import Fleet
from app.models.user import User
from app.models.workspace import (
    WORKSPACE_DEFAULT_ICON,
    Workspace,
)
from app.services.audit import log_audit
from app.services.workspaces import (
    HostedFleets,
    WorkspaceActionRefusedError,
    create_workspace,
    ensure_can_be_company_managed,
    ensure_default_workspace,
    hosted_fleets_of,
    load_workspace_contents,
    make_default_workspace,
    rebind_workspace_knowledge_bases,
    remove_workspace,
    set_workspace_archived,
    unique_slug,
    visible_bound_knowledge_bases,
)

router = APIRouter()

_LABEL = "Workspace"
_NOT_FOUND = f"{_LABEL} not found"

#: The fields of an update that change who can see or change a workspace.
_SHARING_FIELDS = frozenset(
    {
        "is_company_managed",
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
        "access_restricted",
    }
)


def _refused(exc: WorkspaceActionRefusedError) -> HTTPException:
    """The HTTP answer to a refusal of the shared workspace rules."""
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


async def get_visible_workspace(
    db: AsyncSession,
    user: User,
    workspace_id: str,
    user_group_ids: list[str] | None = None,
) -> Workspace:
    """Load a workspace the caller may READ, or the unknown-id 404.

    Reading is the three-tier visibility (own, shared at any tier,
    company-managed; admins everything), not the manage right: a ``read_only``
    grantee and a non-admin on a company-managed workspace see it in the list
    and must be able to open it. Writes go through ``get_manageable_entity``.
    """
    workspace_uuid = parse_path_id(workspace_id, not_found_detail=_NOT_FOUND)
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    workspace = (
        await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_uuid,
                user_visible_filter(
                    Workspace, user.id, user_group_ids, is_admin=user.is_admin
                ),
            )
        )
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return workspace


def _to_response(
    workspace: Workspace,
    *,
    can_manage: bool,
    owner_name: str | None = None,
    contents: dict[str, int] | None = None,
    hosted: HostedFleets | None = None,
) -> WorkspaceResponse:
    hosted = hosted or HostedFleets()
    return WorkspaceResponse(
        id=str(workspace.id),
        user_id=str(workspace.user_id) if workspace.user_id else None,
        owner_name=owner_name,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description or "",
        icon=workspace.icon or WORKSPACE_DEFAULT_ICON,
        color=workspace.color or "",
        instructions=workspace.instructions or "",
        is_default=bool(workspace.is_default),
        is_archived=bool(workspace.is_archived),
        is_company_managed=bool(workspace.is_company_managed),
        shared_group_ids=workspace.shared_group_ids,
        shared_group_permissions=workspace.shared_group_permissions,
        shared_user_ids=workspace.shared_user_ids,
        shared_user_permissions=workspace.shared_user_permissions,
        access_restricted=bool(workspace.access_restricted),
        can_manage=can_manage,
        created_at=workspace.created_at.isoformat() if workspace.created_at else "",
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else "",
        # Absent when nobody counted, which is a different claim from zero —
        # see `WorkspaceContents`.
        contents=WorkspaceContents(**contents) if contents is not None else None,
        hosted_fleets=[
            WorkspaceHostedFleet(
                id=str(f.id), name=f.name, slug=f.slug, is_archived=f.is_archived
            )
            for f in hosted.visible
        ],
        hosted_fleet_count=hosted.total,
        live_hosted_fleet_count=hosted.live,
    )


async def _respond(
    db: AsyncSession,
    user: User,
    workspaces: list[Workspace],
    user_group_ids: list[str] | None = None,
    *,
    with_contents: bool = False,
):
    """Serialise workspaces, optionally with what each one holds.

    `with_contents` is opt-in because the counts cost several queries and only
    the management surface needs them.
    """
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    owner_names = await resolve_owner_names(db, workspaces)
    contents = (
        await load_workspace_contents(db, workspaces, user_id=user.id)
        if with_contents
        else {}
    )
    # One query for every row: the fleets each workspace hosts, named
    # only where the caller can see the fleet.
    hosted = await hosted_fleets_of(
        db,
        [w.id for w in workspaces],
        user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
    )
    return [
        _to_response(
            w,
            can_manage=can_manage(user, w, user_group_ids),
            owner_name=owner_names.get(str(w.user_id)) if w.user_id else None,
            contents=contents.get(str(w.id)),
            hosted=hosted.get(w.id),
        )
        for w in workspaces
    ]


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    include_archived: bool = False,
    with_contents: bool = Query(
        False, description="Count what each workspace holds (the management view)."
    ),
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """List workspaces visible to the caller, creating their default on first use.

    Creating here rather than in a migration is what keeps the migration
    additive and free of an unbounded write. It also means a user who never
    opens the feature never gets a row.

    A default created here is committed before the response is built, so it
    appears in the very response that created it and a client acting on its
    id never reads it as missing (``get_db`` only commits after the response
    is sent).

    The list is the three-tier library set for admins too: every user
    owns a default, so an admin bypass here would list one "General" per
    account. ``scope=all`` lists every workspace for an admin.
    """
    await ensure_default_workspace(db, user.id)
    await db.commit()

    user_group_ids = await get_user_group_ids(db, user.id)
    query = user_visible_query(
        Workspace,
        user.id,
        user_group_ids,
        is_admin=list_scope_admin_bypass(scope, user),
    )
    if not include_archived:
        query = query.where(Workspace.is_archived.is_(False))
    result = await db.execute(query)
    return await _respond(
        db,
        user,
        list(result.scalars().all()),
        user_group_ids,
        with_contents=with_contents,
    )


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace_endpoint(
    body: WorkspaceCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a workspace (admins only for company-managed)."""
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed workspaces"
    )
    workspace = await create_workspace(
        db,
        user_id=user.id,
        name=body.name,
        description=body.description or "",
        icon=body.icon or WORKSPACE_DEFAULT_ICON,
        color=body.color or "",
        instructions=body.instructions,
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, workspace, body, partial=False)
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Created workspace '{workspace.name}'",
        details={
            "slug": workspace.slug,
            "is_company_managed": workspace.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(workspace)
    return _to_response(
        workspace, can_manage=can_manage(user, workspace), owner_name=user.full_name
    )


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Get one workspace (own, shared, company-managed, or admin)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    workspace = await get_visible_workspace(db, user, workspace_id, user_group_ids)
    return (await _respond(db, user, [workspace], user_group_ids))[0]


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: str,
    body: WorkspaceUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Update a workspace (creator or admin only).

    The name may change; the slug does not follow it. A slug that tracked the
    name would break every link pointing at the workspace and buy nothing —
    the id is what everything persisted refers to.
    """
    # Locked: the default rule below is checked against the row as stored. A
    # concurrent `POST /{id}/default` waits for this request to commit, then
    # reads what it wrote and is refused with the reason, and the other way
    # round.
    workspace = await get_manageable_entity(
        db, Workspace, user, workspace_id, action="edit", entity_label=_LABEL, lock=True
    )
    # Only an admin may make a workspace company-managed at all: a non-admin
    # asking is refused as such by `handle_company_managed_toggle` (403), never
    # told about the default rule first (400) - the rule is not theirs to meet.
    if user.is_admin:
        try:
            ensure_can_be_company_managed(workspace, body.is_company_managed)
        except WorkspaceActionRefusedError as exc:
            raise _refused(exc) from None
    sharing_touched = bool(_SHARING_FIELDS & body.model_fields_set)

    if body.name is not None:
        workspace.name = body.name
    if body.description is not None:
        workspace.description = body.description
    if body.icon is not None:
        workspace.icon = body.icon or WORKSPACE_DEFAULT_ICON
    if body.color is not None:
        workspace.color = body.color
    if body.instructions is not None:
        workspace.instructions = body.instructions
    if body.is_archived is not None:
        try:
            await set_workspace_archived(db, workspace, body.is_archived)
        except WorkspaceActionRefusedError as exc:
            raise _refused(exc) from None
    await apply_group_share_fields(db, workspace, body, partial=True)

    await handle_company_managed_toggle(
        db,
        user,
        workspace,
        body.is_company_managed,
        agent_field=None,  # agents never bind a workspace by id
        # Restricting a company-managed workspace in the same request turns
        # its grants into the audience: they must survive the toggle.
        keep_grants=wants_access_restriction(body, workspace),
    )
    # After `is_company_managed` is applied: `access_restricted` is only
    # coherent relative to it.
    await apply_user_share_fields(db, workspace, body, partial=True)

    new_owner_id = safe_uuid(body.user_id)
    if user.is_admin and new_owner_id is not None and new_owner_id != workspace.user_id:
        # The default and the slug are per-owner, and `apply_owner_transfer`
        # flushes: carried over as they are, a default would collide with the
        # recipient's own (or hand them one they did not choose) and a slug
        # they already hold would trip the per-owner unique, a 500 either way.
        # The previous owner gets a fresh default on their next listing.
        workspace.is_default = False
        workspace.slug = await unique_slug(db, new_owner_id, workspace.slug)

    await apply_owner_transfer(
        db,
        actor=user,
        entity=workspace,
        new_owner_id=body.user_id,
        entity_type="workspace",
        entity_name=workspace.name,
        request=request,
    )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Updated workspace '{workspace.name}'",
        details={
            "slug": workspace.slug,
            "is_archived": workspace.is_archived,
            "is_company_managed": workspace.is_company_managed,
            "sharing_changed": sharing_touched,
            "instructions_changed": "instructions" in body.model_fields_set,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(workspace)
    return (await _respond(db, user, [workspace]))[0]


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Delete a workspace (creator or admin only).

    The default is refused: every user must keep exactly one, and choosing
    which of their others inherits it is not a decision this endpoint makes.

    Four kinds of thing can be attached to a workspace and the delete does something
    DIFFERENT to each, because each loses something different:

    * **Conversations** — unfiled: they stay, in no workspace, and are never
      moved to a default workspace nobody chose for them.
    * **Knowledge bases** — a BOUND one is untouched: the binding row
      cascades away and the corpus stays, because a workspace must never take
      somebody's knowledge base with it on the way out. The workspace's OWN
      Sources and Conversations go with it; linked skills, like bound
      knowledge bases, lose only the link.
    * **Worksets** — purged through ``purge_workset``, never cascaded. Only
      that function knows what a workset owns, so a DB-level cascade would
      orphan its knowledge base, documents and vectors.
    * **Work items** — the delete is REFUSED while any remain. Deleting a
      workspace must never delete tracked work, so ``RESTRICT`` is surfaced
      here as a 409 with the count rather than left to surface as a 500.
    """
    workspace = await get_manageable_entity(
        db, Workspace, user, workspace_id, action="delete", entity_label=_LABEL
    )
    try:
        deletion = await remove_workspace(db, workspace)
    except WorkspaceActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Deleted workspace '{workspace.name}'",
        details={
            "slug": workspace.slug,
            "is_company_managed": workspace.is_company_managed,
            "conversations_unfiled": deletion.conversations_unfiled,
            "worksets_purged": deletion.worksets_purged,
            "knowledge_documents_deleted": deletion.knowledge_documents_deleted,
        },
        request=request,
    )
    await db.commit()


@router.post("/{workspace_id}/default", response_model=WorkspaceResponse)
async def set_default_workspace(
    workspace_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Make this workspace the caller's default.

    Two statements, ordered: clear the current default first, then set the new
    one. The reverse order would transiently hold two defaults and trip
    ``uq_workspaces_default_per_user`` — the constraint doing exactly its job,
    on a request that is legitimate.

    An archived workspace is refused: the default is where a user lands, and
    landing in something hidden from every picker is a dead end.
    """
    # Locked, for the same reason as the update: an admin making it
    # company-managed concurrently is seen, not raced.
    workspace = await get_manageable_entity(
        db, Workspace, user, workspace_id, action="edit", entity_label=_LABEL, lock=True
    )
    # The default is a per-user property, and the partial unique is keyed on
    # the OWNER: someone else's shared workspace cannot be yours.
    try:
        await make_default_workspace(db, user.id, workspace)
    except WorkspaceActionRefusedError as exc:
        raise _refused(exc) from None

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Made workspace '{workspace.name}' the default",
        details={"slug": workspace.slug},
        request=request,
    )
    await db.commit()
    await db.refresh(workspace)
    return (await _respond(db, user, [workspace]))[0]


# ── Knowledge-base bindings ─────────────────────────────────────────────────
#
# A binding says "this workspace works with that corpus". It is not ownership:
# the knowledge base keeps its own owner and its own three-tier visibility, so
# binding one grants nothing, and unbinding takes nothing away.
#
# That is why both directions are checked separately. Managing the workspace
# is not enough to bind a corpus — the caller must also be able to SEE the
# corpus, or a workspace would become a way to name knowledge bases you were
# never shown. For the same reason a reader is only ever shown the bound
# corpora THEY can see: a manager who binds their private knowledge base into
# a shared workspace does not publish its name to the workspace's readers.


async def _bound_refs(
    db: AsyncSession,
    workspace: Workspace,
    user: User,
    user_group_ids: list[str],
) -> list[WorkspaceKnowledgeBaseRef]:
    rows = await visible_bound_knowledge_bases(
        db, workspace, user.id, user_group_ids, is_admin=user.is_admin
    )
    return [
        WorkspaceKnowledgeBaseRef(
            knowledge_base_id=str(kb.id),
            name=kb.name,
            description=kb.description or "",
        )
        for kb in rows
    ]


@router.get(
    "/{workspace_id}/knowledge-bases",
    response_model=list[WorkspaceKnowledgeBaseRef],
)
async def list_workspace_knowledge_bases(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The knowledge bases bound to this workspace that the caller can see."""
    user_group_ids = await get_user_group_ids(db, user.id)
    workspace = await get_visible_workspace(db, user, workspace_id, user_group_ids)
    return await _bound_refs(db, workspace, user, user_group_ids)


@router.put(
    "/{workspace_id}/knowledge-bases",
    response_model=list[WorkspaceKnowledgeBaseRef],
)
async def set_workspace_knowledge_bases(
    workspace_id: str,
    body: WorkspaceKnowledgeBasesUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Replace the workspace's knowledge-base bindings with the given set.

    A replace rather than add/remove: a caller sending back the list it
    rendered cannot desynchronise from a concurrent change the way an
    incremental endpoint can, and "unbind the last one" needs no special case.

    Every id ADDED is resolved through the caller's OWN visibility. Being able
    to manage the workspace is not enough — otherwise binding would become a
    way to learn that a knowledge base exists, and to name it in a container
    somebody else can read. An id the caller cannot see is a 404 for that id,
    not a silently dropped entry.

    The replace covers the bindings the caller can see, which is what they
    were shown: a knowledge base another manager bound and this caller cannot
    see is kept, not unbound by a list that could never have named it.
    """
    workspace = await get_manageable_entity(
        db, Workspace, user, workspace_id, action="edit", entity_label=_LABEL
    )
    user_group_ids = await get_user_group_ids(db, user.id)
    wanted = set(parse_uuid_list(body.knowledge_base_ids, "knowledge base"))
    try:
        added, removed = await rebind_workspace_knowledge_bases(
            db,
            workspace,
            user.id,
            user_group_ids,
            is_admin=user.is_admin,
            wanted=wanted,
        )
    except WorkspaceActionRefusedError as exc:
        raise _refused(exc) from None

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="workspace",
        entity_id=workspace.id,
        entity_name=workspace.name,
        summary=f"Updated knowledge bases bound to workspace '{workspace.name}'",
        details={
            "slug": workspace.slug,
            "added_knowledge_base_ids": sorted(str(k) for k in added),
            "removed_knowledge_base_ids": sorted(str(k) for k in removed),
        },
        request=request,
    )
    await db.commit()
    return await _bound_refs(db, workspace, user, user_group_ids)
