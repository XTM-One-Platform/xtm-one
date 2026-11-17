# SPDX-License-Identifier: Apache-2.0

"""Pydantic schemas for the Workspace API.

A Workspace is a named environment scoping an owner's tools, conversations
and resources. The three-tier visibility fields mirror every other
user-authored artifact (``NovaRule`` / ``Prompt`` / ``CustomTool``).

``slug`` is response-only on purpose. It is derived from the name at creation
and then stable — nothing persisted references it, so letting a caller set or
change it would create a second, mutable identity for an entity that already
has one.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    icon: str = Field(default="", max_length=50)
    color: str = Field(default="", max_length=30)
    #: Every agent in a conversation filed here receives them.
    instructions: str = Field(default="", max_length=8000)
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None


class WorkspaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    icon: str | None = Field(default=None, max_length=50)
    color: str | None = Field(default=None, max_length=30)
    #: Every agent in a conversation filed here receives them.
    instructions: str | None = Field(default=None, max_length=8000)
    is_archived: bool | None = None
    is_company_managed: bool | None = None
    user_id: str | None = None  # owner transfer (admin only)
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    # Per-person grants and the company-managed audience restriction,
    # validated by `helpers.apply_user_share_fields` like every other entity.
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


# Declared before `WorkspaceResponse`, whose annotation Pydantic resolves
# when that class is built.
class WorkspaceContents(BaseModel):
    """How much of each scoped thing a workspace holds.

    ``work_items``, ``open_work_items``, ``shared_states`` and ``worksets``
    count the workspace's population, the one its Operations tab lists: filed
    in it, or produced by a home member of the fleet it hosts.
    ``held_work_items`` counts the items filed in it, the ones that block a
    delete.
    """

    conversations: int = 0
    work_items: int = 0
    held_work_items: int = 0
    open_work_items: int = 0
    shared_states: int = 0
    worksets: int = 0
    knowledge_bases: int = 0


class WorkspaceHostedFleet(BaseModel):
    """A fleet the workspace hosts: the workspace is its coordination."""

    id: str
    name: str
    slug: str
    is_archived: bool = False


class WorkspaceResponse(BaseModel):
    id: str
    user_id: str | None
    owner_name: str | None = None
    name: str
    slug: str
    description: str
    icon: str
    color: str
    instructions: str = ""
    is_default: bool
    is_archived: bool
    is_company_managed: bool
    shared_group_ids: list[str] | None
    shared_group_permissions: dict[str, str] | None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool = False
    #: Whether the requesting user may edit this workspace
    #: (``helpers.can_manage``); the UI reads it instead of re-deriving tiers.
    can_manage: bool
    created_at: str
    updated_at: str
    #: What the workspace currently holds, for the management surface.
    #:
    #: Counts rather than the rows themselves: the list renders a dozen
    #: workspaces and a management view needs "is there anything in here",
    #: not the contents. Whoever wants the contents opens the tab that owns
    #: them, filtered on this workspace.
    #:
    #: Absent — not zero — when the caller did not ask for them. A zero would
    #: say "this workspace is empty", which is a different claim from "nobody
    #: counted", and the difference decides whether deleting it is safe.
    contents: WorkspaceContents | None = None
    #: The fleets that coordinate in this workspace and that the caller can
    #: see. A fleet LIVES IN one workspace; the workspace survives the
    #: fleet, not the reverse.
    hosted_fleets: list[WorkspaceHostedFleet] = Field(default_factory=list)
    #: Every fleet hosted here, seen or not: a workspace hosting any fleet is
    #: not deleted (``hosted_fleets_refusal``).
    hosted_fleet_count: int = 0
    #: The live (not archived) ones: a workspace hosting one is not archived
    #: (``hosted_fleets_archive_refusal``).
    live_hosted_fleet_count: int = 0


class WorkspaceKnowledgeBaseRef(BaseModel):
    """A knowledge base bound to a workspace, as the UI needs to render it."""

    knowledge_base_id: str
    name: str
    description: str = ""


class WorkspaceKnowledgeBasesUpdate(BaseModel):
    """The complete set of bindings, not a delta.

    Replace rather than add/remove: a caller sending the list it rendered
    cannot desynchronise from a concurrent change the way an incremental
    endpoint can, and "unbind the last one" needs no special case.
    """

    knowledge_base_ids: list[str] = Field(max_length=500)


# ── Workspace knowledge ──────────────────────────────────────────────


class WorkspaceSourcesCounts(BaseModel):
    count: int = 0
    ready: int = 0
    pending: int = 0
    failed: int = 0


class WorkspaceConversationCounts(BaseModel):
    indexed: int = 0
    pending: int = 0


class WorkspaceKnowledgeSummary(BaseModel):
    """What a workspace gives its agents, as its knowledge panel shows it."""

    instructions: str
    #: Whether the caller may change any of it (the workspace's own right).
    can_manage: bool
    #: The Sources knowledge base, written through the knowledge-base routes;
    #: ``None`` until the first source is added.
    sources_knowledge_base_id: str | None = None
    sources: WorkspaceSourcesCounts
    #: Linked skills and bound knowledge bases the CALLER can see.
    skills: int = 0
    knowledge_bases: int = 0
    #: Filed conversations indexed into the workspace's conversation search,
    #: and those still waiting (never indexed, or new messages since).
    conversations: WorkspaceConversationCounts = Field(default_factory=lambda: WorkspaceConversationCounts())


class WorkspaceSourcesKnowledgeBase(BaseModel):
    knowledge_base_id: str


class WorkspaceSourceFromAttachment(BaseModel):
    file_attachment_id: str = Field(min_length=1, max_length=64)


class WorkspaceSourceAdded(BaseModel):
    knowledge_base_id: str
    document_id: str


class WorkspaceSkillRef(BaseModel):
    """A library skill linked to a workspace, as the UI needs to render it."""

    skill_id: str
    name: str
    description: str = ""


class WorkspaceSkillsUpdate(BaseModel):
    """The complete set of linked skills (replace, like the knowledge bases)."""

    skill_ids: list[str] = Field(max_length=100)
