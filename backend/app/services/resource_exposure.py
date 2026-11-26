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

"""Who can use a resource a person lends to others, and through what.

A person lends a resource whenever something shared carries it:

* an integration's stored credential (an OAuth account, a personal access
  token, an API key, a bot token, a service-account secret),
* an MCP server and its secrets (headers, environment, its own OAuth token),
* a custom tool, whose code runs for whoever uses it,
* a channel bot and its bot token,
* a variable and its value (secret or not),
* a knowledge base, a skill or a prompt an agent, a flow or the General
  Assistant attaches: its runs read it as its owner may, for everyone they
  serve, so attaching one lends it (lent by design, disclosed, never scoped
  to the person chatting).

It is lent by sharing the resource itself (a person, a group, company-wide),
and by every entity that carries it: an agent bound to it (its assignments,
its public web widget, anonymous channel users, A2A partners and the platform
MCP key all act with the agent's bindings), an assignment override or trigger,
a flow node, a knowledge base sync source, a skill's private source, a prompt
or a custom tool referencing a variable, the General Assistant's platform
bindings and a user's own assistant bindings.

This module computes that EXPOSURE, read-only: for a resource's page and its
list card, and for the confirmation shown before a change that widens or
narrows it (sharing, binding, creating, unsharing, unbinding, deleting). It
decides nothing and refuses nothing:

* an integration an agent or an assignment binds acts, for anyone but the
  agent's owner, only when that person may use it or the agent lends it
  (``integration_tools.resolve_agent_integration_bindings``): it reaches the
  callers no account names, who act as the owner, and nobody else it does
  not already reach. Every other resource an entity carries is used by that
  entity's runs as it stands; whether a personal OAuth token travels
  on a direct share is its owner's consent (``app.services.integration_credentials``),
  and a proposed consent is settled by its one write rule,
  ``apply_oauth_share_consent``, run on a copy;
* who may SEE an entity is ``app.api.v1.helpers.user_visible_filter``, whose
  three-tier rule :meth:`AudienceResolver.entity_audience` inverts (pinned by
  ``tests/test_resource_exposure.py``).

Every lookup is bounded (:data:`MAX_ENTITIES_PER_KIND` per kind of entity); a
longer answer says it was cut (``truncated``). A preview does not report a cut
lookup of the carriers its change leaves alone: each reaches the same people
before and after the change, so leaving it out can only name someone already
reached, never hide one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.a2a import A2AExposure
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.flow import Flow
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KBSyncSource, KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.platform_settings import PlatformSettings
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.services.integration_credentials import (
    PER_USER_CREDENTIAL_MODE,
    ShareConsentRefusedError,
    apply_oauth_share_consent,
    canonical_integration_ids,
    flatten_binding_ids,
    flow_integration_refs,
    may_consent_to_share,
    normalize_acting_user_id,
    pipeline_ref_integration_ids,
    record_token_owner_on_transfer,
    requested_share_choice,
    users_seeing_integration,
)
from app.services.variable_resolver import extract_variable_ids

MAX_ENTITIES_PER_KIND = 200
MAX_LISTED_PEOPLE = 20
#: Ids per ``LIKE`` pre-filter statement when scanning JSON / text columns.
_ID_CHUNK = 50
#: Ids per ``IN`` statement when :meth:`AudienceResolver.prime_agents` batches
#: the per-agent lookups.
_PRIME_CHUNK = 500

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

RESOURCE_INTEGRATION = "integration"
RESOURCE_MCP_SERVER = "mcp_server"
RESOURCE_CUSTOM_TOOL = "custom_tool"
RESOURCE_CHANNEL_BOT = "channel_bot"
RESOURCE_VARIABLE = "variable"
RESOURCE_KNOWLEDGE_BASE = "knowledge_base"
RESOURCE_SKILL = "skill"
RESOURCE_PROMPT = "prompt"
#: What a person can lend through something shared.
RESOURCE_KINDS = (
    RESOURCE_INTEGRATION,
    RESOURCE_MCP_SERVER,
    RESOURCE_CUSTOM_TOOL,
    RESOURCE_CHANNEL_BOT,
    RESOURCE_VARIABLE,
    RESOURCE_KNOWLEDGE_BASE,
    RESOURCE_SKILL,
    RESOURCE_PROMPT,
)
#: Resources lent only through what attaches them. A preview of a change to
#: one of them (its own share, unshare or deletion) does not list it: the
#: person changing it sees what they change. What it carries is still listed.
ATTACHED_ONLY_KINDS = frozenset({RESOURCE_KNOWLEDGE_BASE, RESOURCE_SKILL, RESOURCE_PROMPT})

RESOURCE_MODELS: dict[str, Any] = {
    RESOURCE_INTEGRATION: Integration,
    RESOURCE_MCP_SERVER: MCPServer,
    RESOURCE_CUSTOM_TOOL: CustomTool,
    RESOURCE_CHANNEL_BOT: ChannelBot,
    RESOURCE_VARIABLE: Variable,
    RESOURCE_KNOWLEDGE_BASE: KnowledgeBase,
    RESOURCE_SKILL: AgentSkill,
    RESOURCE_PROMPT: Prompt,
}

#: Direct shares of the resource itself.
PATH_COMPANY = "company"
PATH_GROUP = "group"
PATH_PEOPLE = "people"
DIRECT_PATHS = (PATH_COMPANY, PATH_GROUP, PATH_PEOPLE)
#: Entities that carry a resource and lend it to their own audience.
PATH_AGENT = "agent"
PATH_ASSIGNMENT = "assignment"
PATH_FLOW = "flow"
PATH_KNOWLEDGE_BASE = "knowledge_base"
PATH_SKILL = "skill"
PATH_PROMPT = "prompt"
PATH_CUSTOM_TOOL = "custom_tool"
PATH_GENERAL_ASSISTANT = "general_assistant"
PATH_PERSONAL_ASSISTANT = "personal_assistant"
CARRIER_PATHS = (
    PATH_AGENT,
    PATH_ASSIGNMENT,
    PATH_FLOW,
    PATH_KNOWLEDGE_BASE,
    PATH_SKILL,
    PATH_PROMPT,
    PATH_CUSTOM_TOOL,
    PATH_GENERAL_ASSISTANT,
    PATH_PERSONAL_ASSISTANT,
)

#: How a carrier holds the resource.
REASON_BINDING = "binding"
REASON_PIPELINE = "pipeline"
REASON_OVERRIDE = "assignment_override"
REASON_TRIGGER = "trigger"
REASON_FLOW_NODE = "flow_node"
REASON_SYNC_SOURCE = "sync_source"
REASON_SKILL_SOURCE = "skill_source"
REASON_CHANNEL = "channel"
REASON_REFERENCE = "reference"
#: An assignment's runs resolve their agent's per-user (template) binding to
#: the integration of the person they act as (the agent's owner, or the run-as
#: user of a company-managed assignment), else to the company-wide one that
#: person may use.
REASON_PER_USER = "per_user_template"
#: What an agent (or the General Assistant) attaches carries resources into
#: its runs: a prompt's or skill's variables resolve there, a skill's private
#: source feeds what it reads, a knowledge base's sync source what it searches.
REASON_ATTACHED_PROMPT = "attached_prompt"
REASON_ATTACHED_SKILL = "attached_skill"
REASON_ATTACHED_KNOWLEDGE_BASE = "attached_knowledge_base"
_ATTACHMENT_COLUMNS = ("knowledge_base_ids", "skill_ids", "prompt_ids")

#: What a resource holds that its audience acts with.
HOLDS_NONE = "none"
HOLDS_OAUTH_ACCOUNT = "oauth_account"
HOLDS_API_KEY = "api_key"
HOLDS_BOT_TOKEN = "bot_token"  # noqa: S105 - a kind name, not a secret
HOLDS_PLATFORM = "platform_managed"
HOLDS_MCP_OAUTH = "mcp_oauth_token"
HOLDS_MCP_HEADERS = "mcp_headers"
HOLDS_MCP_ENV = "mcp_env"
HOLDS_CODE = "code"
HOLDS_VALUE = "secret_value"
HOLDS_PLAIN_VALUE = "plain_value"
#: A knowledge base's documents: searched and read, written where the agent
#: holds a write grant.
HOLDS_DOCUMENTS = "documents"
#: A skill's or a prompt's instructions (a skill's files included).
HOLDS_INSTRUCTIONS = "instructions"
#: Kinds that lend nothing: no stored credential, or one minted per user.
_LENDS_NOTHING = frozenset({HOLDS_NONE, HOLDS_PLATFORM})

#: Honest caveats a path carries, rendered as one sentence each by the UI.
#: In a chat, people who cannot see an integration bound to an agent use their
#: own connected integration of the same provider instead (the per-user
#: fallback); the agent's runs, public link, channels and A2A partners act with
#: the bound one.
NOTE_CHAT_USES_OWN = "chat_uses_own_integration"
#: A variable resolves only for someone who can see it, or in a run acting as
#: someone who can (the carrier's owner by default); other people's chats leave
#: the placeholder as is.
NOTE_VARIABLE_RESOLVES_FOR_VIEWERS = "variable_resolves_for_viewers_and_runs"
#: The integration is shared on this path without its OAuth account: the
#: people it reaches use their own.
NOTE_ACCOUNT_NOT_SHARED = "account_not_shared"
#: A per-user binding: in a chat each person uses their own integration of the
#: provider, while the assignment's runs use the one of the person they act as,
#: or the company-wide one that person may use.
NOTE_PER_USER_ACTING = "per_user_resolves_to_acting_user"
#: The agent's owner can no longer see the integration (share revoked, owner
#: removed from the group): its runs refuse the bound integration's tools
#: (``integration_credentials.drop_orphaned_bindings``) until the owner's
#: access comes back or the binding is removed.
NOTE_OWNER_LOST_ACCESS = "owner_lost_access"

#: Ways to avoid lending a resource, named for the UI.
OPT_OUT_WITHHOLD_ACCOUNT = "withhold_connected_account"
OPT_OUT_PER_USER = "per_user_binding"
OPT_OUT_COMPANY_RESOURCE = "company_resource"

ENTITY_AGENT = "agent"
ENTITY_ASSIGNMENT = "assignment"
ENTITY_FLOW = "flow"
ENTITY_KNOWLEDGE_BASE = "knowledge_base"
ENTITY_SKILL = "skill"
ENTITY_PROMPT = "prompt"
ENTITY_GENERAL_ASSISTANT = "general_assistant"
#: A group whose members change: everything shared with it reaches them.
ENTITY_GROUP = "group"
#: The platform MCP endpoint: its key holders act with the agents it exposes.
ENTITY_PLATFORM_MCP = "platform_mcp"
#: What a preview can be asked about: every resource, every carrier a person
#: edits, and the two audiences an administrator changes on their own.
ENTITY_TYPES = tuple(
    dict.fromkeys(
        (
            *RESOURCE_KINDS,
            ENTITY_AGENT,
            ENTITY_ASSIGNMENT,
            ENTITY_FLOW,
            ENTITY_KNOWLEDGE_BASE,
            ENTITY_SKILL,
            ENTITY_PROMPT,
            ENTITY_GENERAL_ASSISTANT,
            ENTITY_GROUP,
            ENTITY_PLATFORM_MCP,
        )
    )
)

#: The carrier path an editable entity stands for.
_ENTITY_PATH = {
    ENTITY_AGENT: PATH_AGENT,
    ENTITY_ASSIGNMENT: PATH_ASSIGNMENT,
    ENTITY_FLOW: PATH_FLOW,
    ENTITY_KNOWLEDGE_BASE: PATH_KNOWLEDGE_BASE,
    ENTITY_SKILL: PATH_SKILL,
    ENTITY_PROMPT: PATH_PROMPT,
    RESOURCE_CUSTOM_TOOL: PATH_CUSTOM_TOOL,
    ENTITY_GENERAL_ASSISTANT: PATH_GENERAL_ASSISTANT,
}

_SHARING_FIELDS = (
    "is_company_managed",
    "access_restricted",
    "shared_group_ids",
    "shared_user_ids",
    "shared_group_permissions",
    "shared_user_permissions",
)

#: Fields a preview may propose, per entity type. Anything else is ignored.
_PROPOSABLE: dict[str, tuple[str, ...]] = {
    RESOURCE_INTEGRATION: (*_SHARING_FIELDS, "keep_oauth_credentials_on_share"),
    RESOURCE_MCP_SERVER: _SHARING_FIELDS,
    RESOURCE_CUSTOM_TOOL: (*_SHARING_FIELDS, "js_function", "description"),
    RESOURCE_CHANNEL_BOT: (*_SHARING_FIELDS, "allow_anonymous"),
    RESOURCE_VARIABLE: (*_SHARING_FIELDS, "is_secret"),
    ENTITY_AGENT: (
        *_SHARING_FIELDS,
        "web_enabled",
        "channel_config",
        "integration_bindings",
        "integration_template_bindings",
        "mcp_server_ids",
        "custom_tool_ids",
        *_ATTACHMENT_COLUMNS,
        "persona",
        "guardrails",
        "handover_prompt",
        "human_in_the_loop_prompt",
        "prepare_tool_ref",
        "finalize_tool_ref",
        # ``DELETE /agents/{id}/channel-config/{channel}``: that channel's entry goes.
        "remove_channel",
    ),
    ENTITY_ASSIGNMENT: (
        "integration_bindings",
        "tool_sources",
        "channel_config",
        "prompt",
        "activation_prompt",
        "ai_label_prompt",
        "human_in_the_loop_prompt",
        "trigger_type",
        "trigger_config",
        "enabled",
        "run_as_user_id",
        "run_as_all_users",
    ),
    ENTITY_FLOW: (*_SHARING_FIELDS, "graph", "status"),
    ENTITY_KNOWLEDGE_BASE: (*_SHARING_FIELDS, "sync_source_integration_ids"),
    ENTITY_SKILL: (*_SHARING_FIELDS, "source_integration_id", "content"),
    ENTITY_PROMPT: (*_SHARING_FIELDS, "content"),
    ENTITY_GENERAL_ASSISTANT: (
        "integration_bindings",
        "mcp_server_ids",
        "custom_tool_ids",
        "knowledge_base_ids",
        "channel_config",
        "system_prompt",
    ),
    # Read by their own branches of :func:`preview_change`: nothing is copied.
    ENTITY_GROUP: ("add_user_ids", "remove_user_ids"),
    ENTITY_PLATFORM_MCP: ("mcp_exposed_agent_ids", "has_mcp_key"),
}
#: Fields a change carries that are not columns of the copied entity.
_CHANGE_ONLY_FIELDS = frozenset(
    {
        "keep_oauth_credentials_on_share",
        "sync_source_integration_ids",
        "remove_channel",
        *_PROPOSABLE[ENTITY_GROUP],
        *_PROPOSABLE[ENTITY_PLATFORM_MCP],
    }
)

#: Columns a proposed copy carries, so every rule below reads the same names
#: on a stored row and on its proposed state.
_COPIED_FIELDS = (
    "id",
    "user_id",
    "name",
    "full_name",
    "agent_id",
    "is_company_managed",
    "access_restricted",
    "shared_group_ids",
    "shared_user_ids",
    "shared_group_permissions",
    "shared_user_permissions",
    "is_internal",
    "web_enabled",
    "channel_config",
    "integration_bindings",
    "integration_template_bindings",
    "mcp_server_ids",
    "custom_tool_ids",
    *_ATTACHMENT_COLUMNS,
    "persona",
    "guardrails",
    "handover_prompt",
    "human_in_the_loop_prompt",
    "prepare_tool_ref",
    "finalize_tool_ref",
    "tool_sources",
    "prompt",
    "activation_prompt",
    "ai_label_prompt",
    "trigger_type",
    "trigger_config",
    "enabled",
    "run_as_user_id",
    "run_as_all_users",
    # Whether a non-admin may trigger a company-managed assignment (an
    # agent's default, an assignment's override).
    "hide_runs",
    "graph",
    "status",
    "webhook_secret",
    "source_integration_id",
    "content",
    "js_function",
    "description",
    "system_prompt",
    "provider",
    "auth_type",
    "auth_mode",
    "access_token_enc",
    "refresh_token_enc",
    "token_expires_at",
    "api_key_enc",
    "client_id_enc",
    "client_secret_enc",
    "extra_config_enc",
    "headers_enc",
    "env_enc",
    "oauth_share_acknowledged",
    "oauth_token_owner_id",
    "credential_mode",
    "scopes",
    "is_secret",
    "value",
    "value_enc",
    "allow_anonymous",
    "transport",
    "ga_integration_bindings",
    "ga_mcp_server_ids",
    "ga_custom_tool_ids",
)


def _uuids(values: Iterable[Any] | None) -> set[uuid.UUID]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return set()
    return {p for p in (safe_uuid(str(v)) for v in values if v) if p is not None}


def _canonical(values: Iterable[Any]) -> set[str]:
    return canonical_integration_ids(v for v in values if isinstance(v, (str, uuid.UUID)))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# ---------------------------------------------------------------------------
# Audiences
# ---------------------------------------------------------------------------


@dataclass
class Audience:
    """Who an entity reaches: people, and the audiences no account names.

    ``user_ids`` counts everyone reached (group members expanded);
    ``named_user_ids`` / ``group_ids`` are what is shown by name. With
    ``everyone``, ``excluded_user_ids`` are the users the answer does NOT
    cover (the part another audience already reaches).
    """

    everyone: bool = False
    user_ids: set[uuid.UUID] = field(default_factory=set)
    named_user_ids: set[uuid.UUID] = field(default_factory=set)
    group_ids: set[uuid.UUID] = field(default_factory=set)
    excluded_user_ids: set[uuid.UUID] = field(default_factory=set)
    public_link: bool = False
    anonymous_channels: set[str] = field(default_factory=set)
    mcp_key_holders: bool = False
    a2a_partners: bool = False
    webhook_callers: bool = False

    def merge(self, other: Audience) -> Audience:
        everyone = self.everyone or other.everyone
        excluded: set[uuid.UUID] = set()
        if self.everyone and other.everyone:
            excluded = self.excluded_user_ids & other.excluded_user_ids
        elif self.everyone:
            excluded = self.excluded_user_ids - other.user_ids
        elif other.everyone:
            excluded = other.excluded_user_ids - self.user_ids
        return Audience(
            everyone=everyone,
            user_ids=self.user_ids | other.user_ids,
            named_user_ids=self.named_user_ids | other.named_user_ids,
            group_ids=self.group_ids | other.group_ids,
            excluded_user_ids=excluded,
            public_link=self.public_link or other.public_link,
            anonymous_channels=self.anonymous_channels | other.anonymous_channels,
            mcp_key_holders=self.mcp_key_holders or other.mcp_key_holders,
            a2a_partners=self.a2a_partners or other.a2a_partners,
            webhook_callers=self.webhook_callers or other.webhook_callers,
        )

    def beyond(self, other: Audience) -> Audience:
        """The part of this audience *other* does not already reach."""
        if other.everyone:
            users: set[uuid.UUID] = (
                other.excluded_user_ids - self.excluded_user_ids
                if self.everyone
                else other.excluded_user_ids & self.user_ids
            )
            everyone = False
            excluded: set[uuid.UUID] = set()
            group_ids: set[uuid.UUID] = set()
        else:
            users = self.user_ids - other.user_ids
            everyone = self.everyone
            excluded = (self.excluded_user_ids | other.user_ids) if self.everyone else set()
            group_ids = self.group_ids - other.group_ids
        return Audience(
            everyone=everyone,
            user_ids=users,
            named_user_ids=self.named_user_ids & users,
            group_ids=group_ids,
            excluded_user_ids=excluded,
            public_link=self.public_link and not other.public_link,
            anonymous_channels=self.anonymous_channels - other.anonymous_channels,
            mcp_key_holders=self.mcp_key_holders and not other.mcp_key_holders,
            a2a_partners=self.a2a_partners and not other.a2a_partners,
            webhook_callers=self.webhook_callers and not other.webhook_callers,
        )

    def without(self, user_ids: Iterable[uuid.UUID | None]) -> Audience:
        """This audience minus the given people (the actor, a secret's holder)."""
        drop = {u for u in user_ids if u is not None}
        return Audience(
            everyone=self.everyone,
            user_ids=self.user_ids - drop,
            named_user_ids=self.named_user_ids - drop,
            group_ids=set(self.group_ids),
            excluded_user_ids=(self.excluded_user_ids | drop) if self.everyone else set(),
            public_link=self.public_link,
            anonymous_channels=set(self.anonymous_channels),
            mcp_key_holders=self.mcp_key_holders,
            a2a_partners=self.a2a_partners,
            webhook_callers=self.webhook_callers,
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.everyone
            or self.user_ids
            or self.public_link
            or self.anonymous_channels
            or self.mcp_key_holders
            or self.a2a_partners
            or self.webhook_callers
        )


class AudienceResolver:
    """Audience lookups for one request, cached (group members, bots, totals).

    *members*, *bots* and *mcp_agent_ids* answer instead of the database: the
    state a proposed change gives a group, a channel bot or the platform MCP
    endpoint.
    """

    def __init__(
        self,
        db: AsyncSession,
        *,
        members: dict[uuid.UUID, set[uuid.UUID]] | None = None,
        bots: dict[uuid.UUID, Any] | None = None,
        mcp_agent_ids: set[str] | None = None,
    ) -> None:
        self.db = db
        self._members: dict[uuid.UUID, set[uuid.UUID]] = dict(members or {})
        self._active_total: int | None = None
        self._mcp_agent_ids: set[str] | None = mcp_agent_ids
        self._a2a_agent_ids: dict[str, bool] = {}
        self._webhook_agent_ids: dict[str, bool] = {}
        self._bots: dict[uuid.UUID, ChannelBot | None] = dict(bots or {})
        self._cache: dict[tuple[str, int, int], Audience] = {}

    async def _load_members(self, group_ids: set[uuid.UUID]) -> None:
        missing = [g for g in group_ids if g not in self._members]
        if not missing:
            return
        for g in missing:
            self._members[g] = set()
        rows = await self.db.execute(
            select(GroupMember.group_id, GroupMember.user_id).where(GroupMember.group_id.in_(missing))
        )
        for group_id, user_id in rows.all():
            if user_id is not None:
                self._members.setdefault(group_id, set()).add(user_id)

    async def members(self, group_id: uuid.UUID) -> set[uuid.UUID]:
        await self._load_members({group_id})
        return self._members.get(group_id, set())

    async def active_user_total(self) -> int:
        if self._active_total is None:
            self._active_total = int(
                (await self.db.execute(select(func.count(User.id)).where(User.is_active.isnot(False)))).scalar() or 0
            )
        return self._active_total

    async def entity_audience(self, entity: Any) -> Audience:
        """Everyone ``user_visible_filter`` admits for *entity*, admins aside.

        Own, per-user grants, group grants, and company-wide unless
        ``access_restricted``; an ``is_internal`` row is nobody's to browse,
        and a workspace's own knowledge base reaches its workspace's audience
        (``KnowledgeBase.visibility_parent``).
        """
        if entity is None or getattr(entity, "is_internal", False):
            return Audience()
        from app.api.v1.helpers import owning_workspace_id
        from app.models.workspace import Workspace

        workspace_id = owning_workspace_id(entity)
        if workspace_id is not None:
            return await self.entity_audience(await self.db.get(Workspace, workspace_id))
        audience = Audience(
            everyone=bool(getattr(entity, "is_company_managed", False))
            and getattr(entity, "access_restricted", False) is not True
        )
        owner = normalize_acting_user_id(getattr(entity, "user_id", None))
        if owner is not None:
            audience.user_ids.add(owner)
            audience.named_user_ids.add(owner)
        granted = _uuids(getattr(entity, "shared_user_ids", None))
        audience.user_ids |= granted
        audience.named_user_ids |= granted
        groups = _uuids(getattr(entity, "shared_group_ids", None))
        audience.group_ids |= groups
        await self._load_members(groups)
        for group_id in groups:
            audience.user_ids |= self._members.get(group_id, set())
        return audience

    async def _mcp_exposed(self) -> set[str]:
        if self._mcp_agent_ids is None:
            row = (await self.db.execute(select(PlatformSettings).limit(1))).scalar_one_or_none()
            ids = (
                getattr(row, "mcp_exposed_agent_ids", None) or []
                if row is not None and getattr(row, "mcp_global_api_key_hash", None)
                else []
            )
            self._mcp_agent_ids = {str(p) for p in _uuids(ids)}
        return self._mcp_agent_ids

    async def _a2a_exposed(self, agent_id: uuid.UUID | None) -> bool:
        if agent_id is None:
            return False
        key = str(agent_id)
        if key not in self._a2a_agent_ids:
            live = (
                await self.db.execute(
                    select(A2AExposure.id)
                    .where(A2AExposure.agent_id == agent_id, A2AExposure.revoked_at.is_(None))
                    .limit(1)
                )
            ).scalar_one_or_none()
            self._a2a_agent_ids[key] = live is not None
        return self._a2a_agent_ids[key]

    async def _webhook_assignments(self, agent_id: uuid.UUID | None) -> bool:
        if agent_id is None:
            return False
        key = str(agent_id)
        if key not in self._webhook_agent_ids:
            hit = (
                await self.db.execute(
                    select(Assignment.id)
                    .where(
                        Assignment.agent_id == agent_id,
                        Assignment.trigger_type == "webhook",
                        Assignment.enabled.isnot(False),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            self._webhook_agent_ids[key] = hit is not None
        return self._webhook_agent_ids[key]

    async def _bot(self, bot_id: Any) -> ChannelBot | None:
        parsed = safe_uuid(str(bot_id)) if bot_id else None
        if parsed is None:
            return None
        if parsed not in self._bots:
            self._bots[parsed] = await self.db.get(ChannelBot, parsed)
        return self._bots[parsed]

    async def prime_agents(self, agents: Iterable[Any]) -> None:
        """Answer :meth:`agent_audience`'s lookups for *agents* with one query
        per lookup instead of one round trip per agent: the members of the
        groups they are shared with, a live A2A exposure, an enabled webhook
        assignment, the channel bots their channels name.

        A preview reaches every agent that shares a resource with the changed
        entity (a company integration: the whole fleet), which made those
        per-agent probes an N+1 of several seconds. An agent not primed
        is still looked up on its own, with the same answer.
        """
        agent_ids: set[uuid.UUID] = set()
        group_ids: set[uuid.UUID] = set()
        bot_ids: set[uuid.UUID] = set()
        for agent in agents:
            agent_id = normalize_acting_user_id(getattr(agent, "id", None))
            if agent_id is not None:
                agent_ids.add(agent_id)
            group_ids |= _uuids(getattr(agent, "shared_group_ids", None))
            config = getattr(agent, "channel_config", None)
            if not isinstance(config, dict):
                continue
            for entry in config.values():
                if isinstance(entry, dict) and entry.get("receive_enabled") and entry.get("channel_bot_id"):
                    bot_id = safe_uuid(str(entry["channel_bot_id"]))
                    if bot_id is not None:
                        bot_ids.add(bot_id)
        for chunk in _chunks(sorted(group_ids, key=str), _PRIME_CHUNK):
            await self._load_members(set(chunk))
        await self._prime_agent_flags(
            self._a2a_agent_ids,
            agent_ids,
            lambda chunk: select(A2AExposure.agent_id).where(
                A2AExposure.agent_id.in_(chunk), A2AExposure.revoked_at.is_(None)
            ),
        )
        await self._prime_agent_flags(
            self._webhook_agent_ids,
            agent_ids,
            lambda chunk: select(Assignment.agent_id).where(
                Assignment.agent_id.in_(chunk),
                Assignment.trigger_type == "webhook",
                Assignment.enabled.isnot(False),
            ),
        )
        for chunk in _chunks(sorted((b for b in bot_ids if b not in self._bots), key=str), _PRIME_CHUNK):
            found = {
                bot.id: bot
                for bot in (await self.db.execute(select(ChannelBot).where(ChannelBot.id.in_(chunk)))).scalars().all()
            }
            for bot_id in chunk:
                self._bots[bot_id] = found.get(bot_id)

    async def _prime_agent_flags(
        self, cache: dict[str, bool], agent_ids: set[uuid.UUID], statement: Callable[[list[uuid.UUID]], Any]
    ) -> None:
        """Fill *cache* (agent id -> whether *statement* names it) for the ids it lacks."""
        missing = sorted((i for i in agent_ids if str(i) not in cache), key=str)
        for chunk in _chunks(missing, _PRIME_CHUNK):
            hits = {str(i) for i in (await self.db.execute(statement(chunk).distinct())).scalars().all()}
            for agent_id in chunk:
                cache[str(agent_id)] = str(agent_id) in hits

    async def agent_audience(self, agent: Any) -> Audience:
        """An agent's audience plus the callers no account names.

        Its public web widget (anyone with the link), channels answering
        people without an account (the router's own
        ``_resolve_allow_anonymous``), A2A partners (a live exposure, or the
        ``a2a_exposed`` a proposed copy carries), the platform MCP key, and
        whoever holds the URL of one of its webhook assignments all act with
        the agent's bindings, as its owner.
        """
        audience = await self.entity_audience(agent)
        if agent is None:
            return audience
        if getattr(agent, "web_enabled", False):
            audience.public_link = True
        config = getattr(agent, "channel_config", None)
        if isinstance(config, dict) and config:
            from app.channels.base import ChannelType
            from app.channels.router import _resolve_allow_anonymous

            for channel, entry in config.items():
                if not isinstance(entry, dict) or not entry.get("receive_enabled"):
                    continue
                try:
                    channel_type = ChannelType(channel)
                except ValueError:
                    continue
                bot = await self._bot(entry.get("channel_bot_id"))
                if _resolve_allow_anonymous(bot, agent, channel_type):
                    audience.anonymous_channels.add(channel_type.value)
        agent_id = normalize_acting_user_id(getattr(agent, "id", None))
        if agent_id is not None:
            if str(agent_id) in await self._mcp_exposed():
                audience.mcp_key_holders = True
            audience.a2a_partners = await self._a2a_exposed(agent_id)
            audience.webhook_callers = await self._webhook_assignments(agent_id)
        proposed_a2a = getattr(agent, "a2a_exposed", None)
        if proposed_a2a is not None:
            audience.a2a_partners = bool(proposed_a2a)
        return audience

    async def carrier_audience(self, kind: str, entity: Any, parent: Any = None) -> Audience:
        """Who a carrier reaches; cached per object, so a proposed copy never
        answers for the stored row."""
        cache_key = (kind, id(entity), id(parent))
        if cache_key in self._cache:
            return self._cache[cache_key]
        if kind == PATH_GENERAL_ASSISTANT:
            audience = Audience(everyone=True)
        elif kind == PATH_PERSONAL_ASSISTANT:
            owner = normalize_acting_user_id(getattr(entity, "id", None))
            audience = Audience(
                user_ids={owner} if owner else set(),
                named_user_ids={owner} if owner else set(),
            )
        elif kind == PATH_AGENT:
            audience = await self.agent_audience(entity)
        elif kind == PATH_ASSIGNMENT:
            base = await self.agent_audience(parent)
            audience = Audience().merge(base)
            if getattr(entity, "trigger_type", None) == "webhook" and getattr(entity, "enabled", True) is not False:
                audience.webhook_callers = True
        elif kind == PATH_FLOW:
            audience = await self.entity_audience(entity)
            if getattr(entity, "webhook_secret", None) and getattr(entity, "status", None) == "active":
                audience.webhook_callers = True
        else:
            audience = await self.entity_audience(entity)
        self._cache[cache_key] = audience
        return audience

    async def serialize(self, audience: Audience) -> dict[str, Any]:
        total = await self.active_user_total()
        people = max(total - len(audience.excluded_user_ids), 0) if audience.everyone else len(audience.user_ids)
        named = sorted(audience.named_user_ids, key=str)[:MAX_LISTED_PEOPLE]
        names: dict[uuid.UUID, str] = {}
        if named:
            rows = await self.db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(named)))
            names = {uid: (name or email or "") for uid, name, email in rows.all()}
        groups: list[dict[str, Any]] = []
        if audience.group_ids:
            rows = await self.db.execute(select(Group.id, Group.name).where(Group.id.in_(list(audience.group_ids))))
            await self._load_members(set(audience.group_ids))
            groups = [
                {
                    "id": str(gid),
                    "name": name,
                    "member_count": len(self._members.get(gid, set())),
                }
                for gid, name in sorted(rows.all(), key=lambda r: (r[1] or "").lower())
            ]
        return {
            "everyone": audience.everyone,
            "people_count": people,
            "people": [{"id": str(uid), "name": names[uid]} for uid in named if uid in names],
            "groups": groups,
            "public_link": audience.public_link,
            "anonymous_channels": sorted(audience.anonymous_channels),
            "mcp_key_holders": audience.mcp_key_holders,
            "a2a_partners": audience.a2a_partners,
            "webhook_callers": audience.webhook_callers,
        }


# ---------------------------------------------------------------------------
# What a resource holds
# ---------------------------------------------------------------------------


def _scopes(raw: Any) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    return [p for p in dict.fromkeys(parts) if p][:20]


def describe_secret(kind: str, resource: Any) -> dict[str, Any]:
    """What *resource* holds that its audience acts with, without decrypting.

    ``holder_id`` is the person whose secret it is: for an integration, whoever
    connected its OAuth token (``oauth_token_owner_id`` when the owner changed),
    else its owner. ``account_shared`` says whether the people the
    resource itself is shared with act with the secret: always, except for an
    OAuth integration shared without its owner's consent (the gate's
    ``oauth_share_acknowledged``), and an integration whose people each
    connect their own account (``credential_mode = "per_user"``): its
    stored secret is its holder's only.
    """
    owner = normalize_acting_user_id(getattr(resource, "user_id", None))
    holder = owner
    is_company = bool(getattr(resource, "is_company_managed", False))
    account_shared = True
    scopes: list[str] = []
    if kind == RESOURCE_INTEGRATION:
        auth_type = getattr(resource, "auth_type", None)
        if auth_type == "platform_managed":
            secret = HOLDS_PLATFORM
        elif auth_type == "oauth":
            secret = (
                HOLDS_OAUTH_ACCOUNT
                if getattr(resource, "access_token_enc", None) or getattr(resource, "refresh_token_enc", None)
                else HOLDS_NONE
            )
        elif getattr(resource, "api_key_enc", None) or getattr(resource, "extra_config_enc", None):
            secret = HOLDS_BOT_TOKEN if auth_type == "bot_token" else HOLDS_API_KEY
        else:
            secret = HOLDS_NONE
        token_owner = normalize_acting_user_id(getattr(resource, "oauth_token_owner_id", None))
        if secret == HOLDS_OAUTH_ACCOUNT and token_owner:
            holder = token_owner
        account_shared = (
            secret != HOLDS_OAUTH_ACCOUNT or bool(getattr(resource, "oauth_share_acknowledged", False))
        ) and getattr(resource, "credential_mode", None) != PER_USER_CREDENTIAL_MODE
        scopes = _scopes(getattr(resource, "scopes", None))
    elif kind == RESOURCE_MCP_SERVER:
        if getattr(resource, "auth_mode", None) == "oauth" and (
            getattr(resource, "access_token_enc", None) or getattr(resource, "refresh_token_enc", None)
        ):
            secret = HOLDS_MCP_OAUTH
        elif getattr(resource, "headers_enc", None):
            secret = HOLDS_MCP_HEADERS
        elif getattr(resource, "env_enc", None):
            secret = HOLDS_MCP_ENV
        else:
            secret = HOLDS_NONE
        scopes = _scopes(getattr(resource, "scopes", None))
    elif kind == RESOURCE_CUSTOM_TOOL:
        secret = HOLDS_CODE
    elif kind == RESOURCE_CHANNEL_BOT:
        secret = (
            HOLDS_BOT_TOKEN
            if getattr(resource, "api_key_enc", None)
            or getattr(resource, "client_secret_enc", None)
            or getattr(resource, "extra_config_enc", None)
            else HOLDS_NONE
        )
        # Channel bots are platform-wide resources, managed by admins.
        is_company = True
    elif kind == RESOURCE_VARIABLE:
        secret = HOLDS_VALUE if getattr(resource, "is_secret", False) else HOLDS_PLAIN_VALUE
    elif kind == RESOURCE_KNOWLEDGE_BASE:
        secret = HOLDS_DOCUMENTS
    elif kind in (RESOURCE_SKILL, RESOURCE_PROMPT):
        secret = HOLDS_INSTRUCTIONS
    else:
        secret = HOLDS_NONE
    return {
        "kind": secret,
        "holder_id": str(holder) if holder else None,
        "is_company": is_company,
        "account_shared": account_shared,
        "scopes": scopes,
    }


def lends_something(secret: dict[str, Any]) -> bool:
    """Whether the described resource holds something its audience acts with."""
    return secret["kind"] not in _LENDS_NOTHING


async def direct_audience(
    resolver: AudienceResolver, kind: str, resource: Any, secret: dict[str, Any] | None = None
) -> Audience:
    """Who may use the resource without any other entity.

    Everyone it is visible to, except for an OAuth integration shared without
    its owner's consent: only its account holders use that token (the gate,
    ``oauth_credentials_usable``).
    """
    if resource is None:
        return Audience()
    secret = secret or describe_secret(kind, resource)
    if secret["account_shared"]:
        return await resolver.entity_audience(resource)
    holders = {
        normalize_acting_user_id(getattr(resource, "user_id", None)),
        normalize_acting_user_id(getattr(resource, "oauth_token_owner_id", None)),
    } - {None}
    return Audience(user_ids=set(holders), named_user_ids=set(holders))


# ---------------------------------------------------------------------------
# What a carrier holds
# ---------------------------------------------------------------------------


def _add(held: dict[str, dict[str, set[str]]], kind: str, ids: Iterable[Any], reason: str) -> None:
    for resource_id in _canonical(ids):
        held.setdefault(kind, {}).setdefault(resource_id, set()).add(reason)


def _channel_bot_ids(config: Any) -> list[str]:
    if not isinstance(config, dict):
        return []
    return [
        entry["channel_bot_id"]
        for entry in config.values()
        if isinstance(entry, dict) and isinstance(entry.get("channel_bot_id"), str)
    ]


def _pipeline_custom_tool_ids(*refs: Any) -> list[str]:
    return [
        ref["tool_id"]
        for ref in refs
        if isinstance(ref, dict) and ref.get("source") == "custom_tool" and isinstance(ref.get("tool_id"), str)
    ]


def _flow_custom_tool_ids(graph: Any) -> list[str]:
    """Custom tools a flow graph names by id (a Script node, a Tool capability)."""
    ids: list[str] = []
    if not isinstance(graph, dict):
        return ids
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.get("config") or {}
        if not isinstance(config, dict):
            continue
        if node.get("kind") == "action.script":
            ids.extend(v for v in _as_list(config.get("custom_tool_id")) if isinstance(v, str))
        elif node.get("kind") == "capability.tool":
            # Built-in tools are listed by name, custom tools by id: only ids parse.
            ids.extend(v for v in _as_list(config.get("tool_ids")) if isinstance(v, str))
    return ids


def _variable_ids(*texts: Any) -> list[str]:
    ids: list[str] = []
    for text in texts:
        if isinstance(text, str) and text:
            ids.extend(extract_variable_ids(text))
    return ids


def _graph_text(graph: Any) -> str:
    try:
        return json.dumps(graph, default=str) if graph else ""
    except (TypeError, ValueError):
        return ""


def held_resources(
    path: str,
    entity: Any,
    *,
    sync_source_integration_ids: Iterable[Any] | None = None,
    per_user_integration_ids: Iterable[Any] | None = None,
    attached: dict[str, dict[str, set[str]]] | None = None,
) -> dict[str, dict[str, set[str]]]:
    """The resources *entity* (a carrier of kind *path*) holds, and how.

    ``{resource kind: {canonical id: {reason, ...}}}``. Pure: a knowledge
    base's sync sources are passed in (*sync_source_integration_ids*), and so
    are the integrations an assignment's runs pick through its agent's
    per-user bindings (*per_user_integration_ids*, :func:`per_user_integrations`)
    and what an agent's attached prompts, skills and knowledge bases carry
    (*attached*, :func:`attached_holdings`). An assignment otherwise holds only
    its own overrides; what it inherits is its agent's.
    """
    held: dict[str, dict[str, set[str]]] = {}
    if entity is None:
        return held
    get = lambda name: getattr(entity, name, None)  # noqa: E731
    if path == PATH_AGENT:
        _add(held, RESOURCE_INTEGRATION, flatten_binding_ids(get("integration_bindings")), REASON_BINDING)
        _add(
            held,
            RESOURCE_INTEGRATION,
            pipeline_ref_integration_ids(get("prepare_tool_ref"), get("finalize_tool_ref")),
            REASON_PIPELINE,
        )
        _add(held, RESOURCE_MCP_SERVER, _as_list(get("mcp_server_ids")), REASON_BINDING)
        _add(held, RESOURCE_CUSTOM_TOOL, _as_list(get("custom_tool_ids")), REASON_BINDING)
        _add(
            held,
            RESOURCE_CUSTOM_TOOL,
            _pipeline_custom_tool_ids(get("prepare_tool_ref"), get("finalize_tool_ref")),
            REASON_PIPELINE,
        )
        _add(held, RESOURCE_CHANNEL_BOT, _channel_bot_ids(get("channel_config")), REASON_CHANNEL)
        _add(
            held,
            RESOURCE_VARIABLE,
            _variable_ids(get("persona"), get("guardrails"), get("handover_prompt"), get("human_in_the_loop_prompt")),
            REASON_REFERENCE,
        )
        _add(held, RESOURCE_KNOWLEDGE_BASE, _attachment_ids(entity, "knowledge_base_ids"), REASON_BINDING)
        _add(held, RESOURCE_SKILL, _attachment_ids(entity, "skill_ids"), REASON_BINDING)
        _add(held, RESOURCE_PROMPT, _attachment_ids(entity, "prompt_ids"), REASON_BINDING)
    elif path == PATH_ASSIGNMENT:
        _add(held, RESOURCE_INTEGRATION, flatten_binding_ids(get("integration_bindings")), REASON_OVERRIDE)
        trigger = get("trigger_config")
        if isinstance(trigger, dict):
            _add(held, RESOURCE_INTEGRATION, [trigger.get("integration_id")], REASON_TRIGGER)
        _add(held, RESOURCE_INTEGRATION, _as_list(per_user_integration_ids), REASON_PER_USER)
        _add(held, RESOURCE_MCP_SERVER, _as_list(get("tool_sources")), REASON_OVERRIDE)
        _add(held, RESOURCE_CHANNEL_BOT, _channel_bot_ids(get("channel_config")), REASON_OVERRIDE)
        _add(
            held,
            RESOURCE_VARIABLE,
            _variable_ids(
                get("prompt"),
                get("activation_prompt"),
                get("ai_label_prompt"),
                get("human_in_the_loop_prompt"),
            ),
            REASON_REFERENCE,
        )
    elif path == PATH_FLOW:
        graph = get("graph")
        _add(held, RESOURCE_INTEGRATION, [ref[2] for ref in flow_integration_refs(graph)], REASON_FLOW_NODE)
        _add(held, RESOURCE_CUSTOM_TOOL, _flow_custom_tool_ids(graph), REASON_FLOW_NODE)
        _add(held, RESOURCE_VARIABLE, _variable_ids(_graph_text(graph)), REASON_FLOW_NODE)
        _add(held, RESOURCE_KNOWLEDGE_BASE, _attachment_ids(entity, "knowledge_base_ids"), REASON_FLOW_NODE)
        _add(held, RESOURCE_SKILL, _attachment_ids(entity, "skill_ids"), REASON_FLOW_NODE)
    elif path == PATH_KNOWLEDGE_BASE:
        _add(held, RESOURCE_INTEGRATION, _as_list(sync_source_integration_ids), REASON_SYNC_SOURCE)
    elif path == PATH_SKILL:
        _add(held, RESOURCE_INTEGRATION, [get("source_integration_id")], REASON_SKILL_SOURCE)
        _add(held, RESOURCE_VARIABLE, _variable_ids(get("content")), REASON_REFERENCE)
    elif path == PATH_PROMPT:
        _add(held, RESOURCE_VARIABLE, _variable_ids(get("content")), REASON_REFERENCE)
    elif path == PATH_CUSTOM_TOOL:
        _add(
            held,
            RESOURCE_VARIABLE,
            _variable_ids(get("js_function"), get("description")),
            REASON_REFERENCE,
        )
    elif path == PATH_GENERAL_ASSISTANT:
        _add(held, RESOURCE_INTEGRATION, flatten_binding_ids(get("integration_bindings")), REASON_BINDING)
        _add(held, RESOURCE_MCP_SERVER, _as_list(get("mcp_server_ids")), REASON_BINDING)
        _add(held, RESOURCE_CUSTOM_TOOL, _as_list(get("custom_tool_ids")), REASON_BINDING)
        _add(held, RESOURCE_CHANNEL_BOT, _channel_bot_ids(get("channel_config")), REASON_CHANNEL)
        _add(held, RESOURCE_VARIABLE, _variable_ids(get("system_prompt")), REASON_REFERENCE)
        _add(held, RESOURCE_KNOWLEDGE_BASE, _attachment_ids(entity, "knowledge_base_ids"), REASON_BINDING)
    elif path == PATH_PERSONAL_ASSISTANT:
        _add(held, RESOURCE_INTEGRATION, flatten_binding_ids(get("ga_integration_bindings")), REASON_BINDING)
        _add(held, RESOURCE_MCP_SERVER, _as_list(get("ga_mcp_server_ids")), REASON_BINDING)
        _add(held, RESOURCE_CUSTOM_TOOL, _as_list(get("ga_custom_tool_ids")), REASON_BINDING)
    for kind, ids in (attached or {}).items():
        for resource_id, reasons in ids.items():
            held.setdefault(kind, {}).setdefault(resource_id, set()).update(reasons)
    return held


async def attached_holdings(
    db: AsyncSession,
    entities: Iterable[Any],
    *,
    prompts: dict[str, Any] | None = None,
    skills: dict[str, Any] | None = None,
    kb_sync: dict[str, list[str]] | None = None,
) -> dict[int, dict[str, dict[str, set[str]]]]:
    """For each agent or General Assistant, keyed by ``id(entity)``: the
    resources its attached prompts and skills reference, its skills' private
    sources and its knowledge bases' sync sources.

    *prompts*, *skills* and *kb_sync* (id -> row, ``None`` for a deleted one;
    knowledge base id -> integration ids) answer instead of the database for
    what a proposed change edits.
    """
    entities = [e for e in entities if e is not None]
    wanted = {column: set() for column in _ATTACHMENT_COLUMNS}
    for entity in entities:
        for column in _ATTACHMENT_COLUMNS:
            wanted[column] |= _attachment_ids(entity, column)
    prompts = dict(prompts or {})
    skills = dict(skills or {})
    kb_sync = dict(kb_sync or {})
    missing = [uuid.UUID(i) for i in wanted["prompt_ids"] - set(prompts)]
    if missing:
        for row in (await db.execute(select(Prompt).where(Prompt.id.in_(missing)))).scalars().all():
            prompts[str(row.id)] = row
    missing = [uuid.UUID(i) for i in wanted["skill_ids"] - set(skills)]
    if missing:
        for row in (await db.execute(select(AgentSkill).where(AgentSkill.id.in_(missing)))).scalars().all():
            skills[str(row.id)] = row
    kb_sync.update(await _kb_sync_map(db, wanted["knowledge_base_ids"] - set(kb_sync)))
    out: dict[int, dict[str, dict[str, set[str]]]] = {}
    for entity in entities:
        held: dict[str, dict[str, set[str]]] = {}
        for prompt_id in _attachment_ids(entity, "prompt_ids"):
            prompt = prompts.get(prompt_id)
            if prompt is not None:
                _add(held, RESOURCE_VARIABLE, _variable_ids(getattr(prompt, "content", None)), REASON_ATTACHED_PROMPT)
        for skill_id in _attachment_ids(entity, "skill_ids"):
            skill = skills.get(skill_id)
            if skill is not None:
                _add(held, RESOURCE_VARIABLE, _variable_ids(getattr(skill, "content", None)), REASON_ATTACHED_SKILL)
                _add(held, RESOURCE_INTEGRATION, [getattr(skill, "source_integration_id", None)], REASON_ATTACHED_SKILL)
        for kb_id in _attachment_ids(entity, "knowledge_base_ids"):
            _add(held, RESOURCE_INTEGRATION, kb_sync.get(kb_id, []), REASON_ATTACHED_KNOWLEDGE_BASE)
        out[id(entity)] = held
    return out


#: A flow attaches skills and knowledge bases through capability nodes.
_FLOW_ATTACHMENT_NODES = {"capability.skill": "skill_ids", "capability.knowledge_base": "knowledge_base_ids"}


def _attachment_ids(entity: Any, column: str) -> set[str]:
    """The knowledge bases, skills or prompts (*column*) *entity* attaches:
    an agent's or the General Assistant's column, a flow's capability nodes."""
    graph = getattr(entity, "graph", None)
    if not isinstance(graph, dict):
        return _canonical(_as_list(getattr(entity, column, None)))
    ids: list[Any] = []
    for node in graph.get("nodes") or []:
        if isinstance(node, dict) and _FLOW_ATTACHMENT_NODES.get(node.get("kind")) == column:
            config = node.get("config")
            if isinstance(config, dict):
                ids.extend(_as_list(config.get(column)))
    return _canonical(ids)


def _per_user_request(assignment: Any, agent: Any) -> tuple[uuid.UUID | None, set[str]] | None:
    """(whom an assignment's runs act as, the providers they resolve per user),
    or ``None`` when its runs resolve nothing that way.

    The assignment engine's rule (``assignment_engine._execution_identity``):
    a run nobody starts acts as the assignment's run-as user (company-managed
    only), else its own user, else its agent's owner, and with none as nobody
    (company-wide integrations only); a per-user fan-out acts as each user
    in turn with what that user may use, so it lends nobody's; a provider the
    run has a fixed binding for is not resolved per user.
    """
    if assignment is None or agent is None:
        return None
    providers = {p for p in _as_list(getattr(agent, "integration_template_bindings", None)) if isinstance(p, str) and p}
    fixed = getattr(assignment, "integration_bindings", None)
    if fixed is None:
        fixed = getattr(agent, "integration_bindings", None)
    if isinstance(fixed, dict):
        providers -= {key for key, ids in fixed.items() if ids}
    company = bool(getattr(agent, "is_company_managed", False))
    if not providers or (company and getattr(assignment, "run_as_all_users", False)):
        return None
    run_as = normalize_acting_user_id(getattr(assignment, "run_as_user_id", None)) if company else None
    acting = run_as or normalize_acting_user_id(getattr(assignment, "user_id", None))
    return acting or normalize_acting_user_id(getattr(agent, "user_id", None)), providers


class PerUserPicks(dict[int, list[str]]):
    """:func:`per_user_integrations`' answer; ``truncated`` when its lookup was cut."""

    truncated: bool = False


async def per_user_integrations(db: AsyncSession, pairs: Iterable[tuple[Any, Any]]) -> PerUserPicks:
    """For each ``(assignment, agent)``, keyed by ``id(assignment)``: the
    integrations its runs pick through the agent's per-user bindings
    (``integration_template_bindings``), as the assignment engine picks them.

    Per provider: the acting person's own integrations (whatever their status:
    a reconnect puts them back in use); while none of them is connected, the
    company-wide one the run falls back to, the first created among those
    ``integration_tools.template_candidate_filter`` admits for that person; and
    every candidate when one carries a ``scope_config``, since the trigger then
    decides which one runs. A fallback is never inferred from a cut lookup of
    the person's own rows (``truncated`` says the answer is incomplete).
    """
    from app.services.integration_tools import template_candidate_filter
    from app.services.integration_user_credentials import CREDENTIAL_MODE_PER_USER, connected_for_clause

    requests: dict[int, tuple[uuid.UUID | None, set[str], bool]] = {}
    for assignment, agent in pairs:
        request = _per_user_request(assignment, agent)
        if request is not None:
            requests[id(assignment)] = (*request, bool(getattr(agent, "is_company_managed", False)))
    picks = PerUserPicks()
    if not requests:
        return picks
    limit = MAX_ENTITIES_PER_KIND
    providers = sorted(set().union(*(wanted for _, wanted, _ in requests.values())))
    actors = sorted({acting for acting, _, _ in requests.values() if acting is not None}, key=str)
    own_rows = (
        (
            await db.execute(
                select(
                    Integration.id,
                    Integration.user_id,
                    Integration.provider,
                    Integration.status,
                    Integration.scope_config,
                )
                .where(
                    Integration.user_id.in_(actors),
                    Integration.provider.in_(providers),
                    Integration.is_company_managed.isnot(True),
                )
                .order_by(case((Integration.status == "connected", 0), else_=1), Integration.created_at, Integration.id)
                .limit(limit + 1)
            )
        ).all()
        if actors
        else []
    )
    truncated = len(own_rows) > limit
    # Connected rows are read first, so a cut lookup still says who has none
    # connected unless the cut fell among the connected ones.
    connected_known = not truncated or own_rows[limit - 1].status != "connected"
    owned: dict[tuple[uuid.UUID, str], list[Any]] = {}
    for row in own_rows[:limit]:
        owned.setdefault((row.user_id, row.provider), []).append(row)
    wide_providers = set(
        (
            await db.execute(
                select(Integration.provider)
                .where(
                    Integration.provider.in_(providers),
                    # A per_user row can serve a person while its holder is disconnected.
                    or_(Integration.status == "connected", Integration.credential_mode == CREDENTIAL_MODE_PER_USER),
                    # access_restricted: bypass - which providers have a company-wide row at all; the query below decides.
                    or_(Integration.is_company_managed.is_(True), Integration.auth_type == "platform_managed"),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    fallbacks: dict[tuple[uuid.UUID | None, bool], dict[str, list[Any]]] = {}
    for acting, company in sorted({(a, c) for a, _, c in requests.values()}, key=lambda k: (str(k[0]), k[1])):
        wanted = wide_providers & set().union(*(w for a, w, c in requests.values() if (a, c) == (acting, company)))
        if not wanted:
            continue
        rows = (
            await db.execute(
                select(Integration.id, Integration.provider, Integration.scope_config)
                .where(
                    Integration.provider.in_(sorted(wanted)),
                    connected_for_clause(acting),
                    await template_candidate_filter(db, acting, company_automation=company),
                    # access_restricted: honoured by template_candidate_filter; this keeps the person's own rows out.
                    or_(Integration.user_id.is_distinct_from(acting), Integration.is_company_managed.is_(True)),
                )
                .order_by(Integration.created_at, Integration.id)
                .limit(limit + 1)
            )
        ).all()
        truncated = truncated or len(rows) > limit
        for row in rows[:limit]:
            fallbacks.setdefault((acting, company), {}).setdefault(row.provider, []).append(row)
    for key, (acting, wanted, company) in requests.items():
        ids: set[str] = set()
        for provider in wanted:
            own = owned.get((acting, provider), []) if acting is not None else []
            connected = [row for row in own if row.status == "connected"]
            fallback = fallbacks.get((acting, company), {}).get(provider, [])
            candidates = connected + fallback
            ids.update(str(row.id) for row in own)
            if not connected_known:
                continue
            if len(candidates) > 1 and any(row.scope_config for row in candidates):
                ids.update(str(row.id) for row in fallback)
            elif fallback and not connected:
                ids.add(str(fallback[0].id))
        picks[key] = sorted(ids)
    picks.truncated = truncated
    return picks


# ---------------------------------------------------------------------------
# Who references a resource
# ---------------------------------------------------------------------------


@dataclass
class Reference:
    """One carrier holding a resource: *kind* is the path kind."""

    kind: str
    entity: Any
    reasons: set[str] = field(default_factory=set)
    #: An assignment's agent, whose audience the assignment reaches.
    parent: Any = None

    @property
    def entity_key(self) -> tuple[str, str]:
        return (self.kind, str(getattr(self.entity, "id", "")))


@dataclass
class References:
    """Carriers holding each resource id, keyed by canonical id."""

    by_resource: dict[str, list[Reference]] = field(default_factory=dict)
    truncated: bool = False

    def add(self, resource_id: str, ref: Reference) -> None:
        refs = self.by_resource.setdefault(resource_id, [])
        for existing in refs:
            if existing.kind == ref.kind and existing.entity is ref.entity:
                existing.reasons |= ref.reasons
                return
        refs.append(ref)

    def add_stored(self, resource_id: str, ref: Reference) -> None:
        """Add a carrier as stored unless the lookup already read it (matched
        on its id: the change's own row may not be the object it read)."""
        refs = self.by_resource.setdefault(resource_id, [])
        if not any(existing.entity_key == ref.entity_key for existing in refs):
            refs.append(ref)

    def of(self, resource_id: Any) -> list[Reference]:
        return list(self.by_resource.get(str(resource_id), []))

    def all(self) -> list[Reference]:
        return [ref for refs in self.by_resource.values() for ref in refs]


def _carrier_agents(refs: Iterable[Reference]) -> list[Any]:
    """The agents whose audience *refs* reach: an agent path's agent, an
    assignment's parent (what :meth:`AudienceResolver.prime_agents` batches)."""
    return [
        ref.parent if ref.kind == PATH_ASSIGNMENT else ref.entity
        for ref in refs
        if ref.kind in (PATH_AGENT, PATH_ASSIGNMENT)
    ]


def _chunks(ids: list[Any], size: int = _ID_CHUNK) -> Iterable[list[Any]]:
    for start in range(0, len(ids), size):
        yield ids[start : start + size]


def _contains_any(columns: Iterable[Any], ids: list[str]) -> Any:
    return or_(*[cast(col, Text).ilike(f"%{i}%") for col in columns for i in ids])


#: Carriers scanned for each resource kind: (path, model, pre-filter columns).
#: The pre-filter is a cheap text match; :func:`held_resources` then decides.
_SCANNED: dict[str, tuple[tuple[str, Any, tuple[str, ...]], ...]] = {
    RESOURCE_INTEGRATION: (
        (PATH_AGENT, Agent, ("integration_bindings", "prepare_tool_ref", "finalize_tool_ref")),
        (PATH_ASSIGNMENT, Assignment, ("integration_bindings", "trigger_config")),
        (PATH_FLOW, Flow, ("graph",)),
        (PATH_PERSONAL_ASSISTANT, User, ("ga_integration_bindings",)),
    ),
    RESOURCE_MCP_SERVER: (
        (PATH_AGENT, Agent, ("mcp_server_ids",)),
        (PATH_ASSIGNMENT, Assignment, ("tool_sources",)),
        (PATH_PERSONAL_ASSISTANT, User, ("ga_mcp_server_ids",)),
    ),
    RESOURCE_CUSTOM_TOOL: (
        (PATH_AGENT, Agent, ("custom_tool_ids", "prepare_tool_ref", "finalize_tool_ref")),
        (PATH_FLOW, Flow, ("graph",)),
        (PATH_PERSONAL_ASSISTANT, User, ("ga_custom_tool_ids",)),
    ),
    RESOURCE_CHANNEL_BOT: (
        (PATH_AGENT, Agent, ("channel_config",)),
        (PATH_ASSIGNMENT, Assignment, ("channel_config",)),
    ),
    RESOURCE_VARIABLE: (
        (PATH_AGENT, Agent, ("persona", "guardrails", "handover_prompt", "human_in_the_loop_prompt")),
        (
            PATH_ASSIGNMENT,
            Assignment,
            ("prompt", "activation_prompt", "ai_label_prompt", "human_in_the_loop_prompt"),
        ),
        (PATH_FLOW, Flow, ("graph",)),
        (PATH_SKILL, AgentSkill, ("content",)),
        (PATH_PROMPT, Prompt, ("content",)),
        (PATH_CUSTOM_TOOL, CustomTool, ("js_function", "description")),
    ),
    RESOURCE_KNOWLEDGE_BASE: (
        (PATH_AGENT, Agent, ("knowledge_base_ids",)),
        (PATH_FLOW, Flow, ("graph",)),
    ),
    RESOURCE_SKILL: (
        (PATH_AGENT, Agent, ("skill_ids",)),
        (PATH_FLOW, Flow, ("graph",)),
    ),
    RESOURCE_PROMPT: ((PATH_AGENT, Agent, ("prompt_ids",)),),
}


async def find_references(db: AsyncSession, kind: str, resource_ids: Iterable[Any]) -> References:
    """Every carrier holding one of *resource_ids* (of *kind*), and how."""
    wanted = sorted(_canonical(resource_ids))
    refs = References()
    if not wanted or kind not in RESOURCE_KINDS:
        return refs
    wanted_set = set(wanted)
    limit = MAX_ENTITIES_PER_KIND
    agents: dict[uuid.UUID, Agent] = {}

    for path, model, column_names in _SCANNED.get(kind, ()):
        columns = [getattr(model, name) for name in column_names]
        seen: dict[Any, Any] = {}
        for chunk in _chunks(wanted):
            rows = (
                (await db.execute(select(model).where(_contains_any(columns, chunk)).limit(limit + 1))).scalars().all()
            )
            refs.truncated |= len(rows) > limit
            for row in rows[:limit]:
                seen.setdefault(row.id, row)
        if path == PATH_AGENT:
            agents.update(seen)
        if path == PATH_ASSIGNMENT:
            parent_ids = {a.agent_id for a in seen.values()} - set(agents)
            if parent_ids:
                for agent in (await db.execute(select(Agent).where(Agent.id.in_(list(parent_ids))))).scalars().all():
                    agents[agent.id] = agent
        for row in seen.values():
            held = held_resources(path, row).get(kind, {})
            parent = agents.get(row.agent_id) if path == PATH_ASSIGNMENT else None
            if path == PATH_ASSIGNMENT and parent is None:
                continue
            for resource_id in set(held) & wanted_set:
                refs.add(resource_id, Reference(path, row, set(held[resource_id]), parent))

    if kind == RESOURCE_INTEGRATION:
        wanted_uuids = [uuid.UUID(i) for i in wanted]
        rows = (
            await db.execute(
                select(KBSyncSource.integration_id, KnowledgeBase)
                .join(KnowledgeBase, KnowledgeBase.id == KBSyncSource.knowledge_base_id)
                .where(KBSyncSource.integration_id.in_(wanted_uuids))
                .limit(limit + 1)
            )
        ).all()
        refs.truncated |= len(rows) > limit
        kbs: dict[uuid.UUID, KnowledgeBase] = {}
        for integration_id, kb in rows[:limit]:
            kb = kbs.setdefault(kb.id, kb)
            refs.add(str(integration_id), Reference(PATH_KNOWLEDGE_BASE, kb, {REASON_SYNC_SOURCE}))
        skills = (
            (
                await db.execute(
                    select(AgentSkill).where(AgentSkill.source_integration_id.in_(wanted_uuids)).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        refs.truncated |= len(skills) > limit
        for skill in skills[:limit]:
            refs.add(
                str(skill.source_integration_id),
                Reference(PATH_SKILL, skill, {REASON_SKILL_SOURCE}),
            )
        refs.truncated |= await _add_per_user_references(db, refs, wanted_uuids, wanted_set)

    ga = (await db.execute(select(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
    if ga is not None:
        held = held_resources(PATH_GENERAL_ASSISTANT, ga).get(kind, {})
        for resource_id in set(held) & wanted_set:
            refs.add(resource_id, Reference(PATH_GENERAL_ASSISTANT, ga, set(held[resource_id])))
    refs.truncated |= await _add_attached_references(db, refs, kind, wanted_set, ga)
    return refs


_ATTACHED_CARRIERS = (PATH_KNOWLEDGE_BASE, PATH_SKILL, PATH_PROMPT)


async def _add_attached_references(
    db: AsyncSession, refs: References, kind: str, wanted_set: set[str], ga: Any
) -> bool:
    """The agents, flows and the General Assistant attaching a knowledge base,
    a skill or a prompt that holds one of the wanted resources; whether the
    lookup was cut."""
    attached = {path: {str(r.entity.id) for r in refs.all() if r.kind == path} for path in _ATTACHED_CARRIERS}
    ids = sorted(set().union(*attached.values()))
    if not ids:
        return False
    limit = MAX_ENTITIES_PER_KIND
    truncated = False
    carriers: list[tuple[str, Any]] = []
    for path, model, columns in (
        (PATH_AGENT, Agent, [getattr(Agent, column) for column in _ATTACHMENT_COLUMNS]),
        (PATH_FLOW, Flow, [Flow.graph]),
    ):
        seen: dict[uuid.UUID, Any] = {}
        for chunk in _chunks(ids):
            rows = (
                (await db.execute(select(model).where(_contains_any(columns, chunk)).limit(limit + 1))).scalars().all()
            )
            truncated |= len(rows) > limit
            for row in rows[:limit]:
                seen.setdefault(row.id, row)
        carriers.extend((path, row) for row in seen.values())
    if ga is not None and _attachment_ids(ga, "knowledge_base_ids") & attached[PATH_KNOWLEDGE_BASE]:
        carriers.append((PATH_GENERAL_ASSISTANT, ga))
    held = await attached_holdings(db, [carrier for _, carrier in carriers])
    for path, carrier in carriers:
        for resource_id, reasons in held.get(id(carrier), {}).get(kind, {}).items():
            if resource_id in wanted_set:
                refs.add(resource_id, Reference(path, carrier, set(reasons)))
    return truncated


async def _add_per_user_references(
    db: AsyncSession, refs: References, wanted_uuids: list[uuid.UUID], wanted_set: set[str]
) -> bool:
    """The assignments whose runs pick one of the wanted integrations through
    their agent's per-user bindings (:func:`per_user_integrations`: a private
    one as its owner, a company-wide one as anyone's fallback); whether the
    lookup was cut."""
    wanted = (
        await db.execute(
            select(
                Integration.user_id, Integration.provider, Integration.is_company_managed, Integration.auth_type
            ).where(Integration.id.in_(wanted_uuids))
        )
    ).all()
    personal = [(owner, provider) for owner, provider, company, _auth in wanted if provider and owner and not company]
    wide = sorted(
        {provider for _owner, provider, company, auth in wanted if provider and (company or auth == "platform_managed")}
    )
    carriers = []
    if personal:
        owner_ids = sorted({owner for owner, _ in personal}, key=str)
        carriers.append(
            and_(
                or_(
                    Assignment.run_as_user_id.in_(owner_ids),
                    Assignment.user_id.in_(owner_ids),
                    Agent.user_id.in_(owner_ids),
                ),
                _contains_any((Agent.integration_template_bindings,), sorted({p for _, p in personal})),
            )
        )
    if wide:
        carriers.append(_contains_any((Agent.integration_template_bindings,), wide))
    if not carriers:
        return False
    limit = MAX_ENTITIES_PER_KIND
    rows = (
        await db.execute(
            select(Assignment, Agent)
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(or_(*carriers))
            .limit(limit + 1)
        )
    ).all()
    pairs = [(assignment, agent) for assignment, agent in rows[:limit]]
    picked = await per_user_integrations(db, pairs)
    for assignment, agent in pairs:
        for integration_id in set(picked.get(id(assignment), [])) & wanted_set:
            refs.add(integration_id, Reference(PATH_ASSIGNMENT, assignment, {REASON_PER_USER}, agent))
    return len(rows) > limit or picked.truncated


async def _kb_sync_integration_ids(db: AsyncSession, kb_id: Any) -> list[str]:
    parsed = safe_uuid(str(kb_id)) if kb_id else None
    if parsed is None:
        return []
    rows = await db.execute(select(KBSyncSource.integration_id).where(KBSyncSource.knowledge_base_id == parsed))
    return [str(i) for i in rows.scalars().all() if i is not None]


# ---------------------------------------------------------------------------
# Whether a path lends the secret
# ---------------------------------------------------------------------------


def _per_user(kind: str, resource: Any) -> bool:
    return kind == RESOURCE_INTEGRATION and getattr(resource, "credential_mode", None) == PER_USER_CREDENTIAL_MODE


def _acting_owner(ref: Reference) -> uuid.UUID | None:
    """Whom a carrier's runs act as when no person of the audience does: its
    owner (an assignment's, its agent's owner)."""
    entity = ref.parent if ref.kind == PATH_ASSIGNMENT else ref.entity
    return normalize_acting_user_id(getattr(entity, "user_id", None))


async def path_usable(db: AsyncSession, kind: str, resource: Any, ref: Reference, secret: dict[str, Any]) -> bool:
    """Whether the resource's secret travels on *ref*.

    Every carrier uses what it holds as bound, an integration's personal OAuth
    token included: nothing is withheld at run time (maintainer decision of
    2026-09-27). Except an integration shared
    without its connected account (``per_user``): every run acts with
    the account of the person it acts as, so the stored one travels only on a
    carrier whose runs act as its holder.
    """
    if not lends_something(secret):
        return False
    if _per_user(kind, resource):
        holder = safe_uuid(secret["holder_id"]) if secret["holder_id"] else None
        return holder is not None and _acting_owner(ref) == holder
    return True


async def lent_audience(
    resolver: AudienceResolver, kind: str, resource: Any, ref: Reference, secret: dict[str, Any]
) -> Audience:
    """Who acts with the resource's secret through *ref* (empty when nobody does).

    The carrier's audience, except for an agent's chats on an integration:

    * a ``per_user`` integration: its chats act with each person's
      own account, so of an agent's audience only the callers no account
      names (its public link, anonymous channels, A2A partners, MCP key
      holders, webhook callers), who act as its owner, use the holder's;
    * an integration the agent only binds (:func:`_chat_uses_own`): a
      chat turn or an assignment run of someone who cannot see it drops it,
      and whoever can see it is already in its own audience, so the agent
      lends it to those callers and to its owner, whose own runs use it;
    * an integration an assignment's own bindings name
      (:func:`_override_uses_own`): its runs drop it for whoever cannot see
      it, its webhook callers aside, who act as the person it runs as.
    """
    if not await path_usable(resolver.db, kind, resource, ref, secret):
        return Audience()
    audience = await resolver.carrier_audience(ref.kind, ref.entity, ref.parent)
    if _per_user(kind, resource) and ref.kind == PATH_AGENT:
        return _callers(audience)
    if _chat_uses_own(kind, resource, ref):
        owner = normalize_acting_user_id(getattr(ref.entity, "user_id", None))
        owners = {owner} & audience.user_ids if owner else set()
        return _callers(audience).merge(
            Audience(user_ids=owners, named_user_ids=owners & audience.named_user_ids)
        )
    if _override_uses_own(kind, resource, ref):
        return Audience(webhook_callers=audience.webhook_callers)
    return audience


def _callers(audience: Audience) -> Audience:
    """The part of an agent's audience no account names: they act as its owner."""
    return Audience(
        public_link=audience.public_link,
        anonymous_channels=set(audience.anonymous_channels),
        mcp_key_holders=audience.mcp_key_holders,
        a2a_partners=audience.a2a_partners,
        webhook_callers=audience.webhook_callers,
    )


#: How an agent holds an integration its chats drop for someone who cannot
#: see it (``integration_tools.shadow_invisible_bindings``): its bindings and
#: its pipeline pins; what it attaches is read on the attachment's own terms.
_CHAT_SHADOWED_REASONS = frozenset({REASON_BINDING, REASON_PIPELINE})


def _override_uses_own(kind: str, resource: Any, ref: Reference) -> bool:
    """Whether *ref* is an assignment that holds the integration only through
    its own bindings, which its runs use for the people who can see it and
    replace with their own for everyone else."""
    return (
        kind == RESOURCE_INTEGRATION
        and ref.kind == PATH_ASSIGNMENT
        and not _per_user(kind, resource)
        and bool(ref.reasons)
        and ref.reasons <= {REASON_OVERRIDE}
    )


def _chat_uses_own(kind: str, resource: Any, ref: Reference) -> bool:
    """Whether *ref* is an agent that holds the integration only through its
    bindings, which its chats use for the people who can see it and replace
    with their own for everyone else."""
    return (
        kind == RESOURCE_INTEGRATION
        and ref.kind == PATH_AGENT
        and not _per_user(kind, resource)
        and bool(ref.reasons)
        and ref.reasons <= _CHAT_SHADOWED_REASONS
    )


async def orphaned_agent_ids(db: AsyncSession, kind: str, resource: Any, refs: Iterable[Reference]) -> set[str]:
    """Ids of the agents among *refs* that bind the integration *resource*
    while their owner can no longer see it: an orphaned binding, refused at
    run time by ``integration_credentials.drop_orphaned_bindings``. A
    company-managed or owner-less agent orphans nothing."""
    if kind != RESOURCE_INTEGRATION or getattr(resource, "id", None) is None:
        return set()
    owners: dict[str, uuid.UUID] = {}
    for ref in refs:
        if ref.kind != PATH_AGENT or REASON_BINDING not in ref.reasons:
            continue
        if getattr(ref.entity, "is_company_managed", False):
            continue
        owner = normalize_acting_user_id(getattr(ref.entity, "user_id", None))
        if owner is not None and getattr(ref.entity, "id", None) is not None:
            owners[str(ref.entity.id)] = owner
    if not owners:
        return set()
    seeing = await users_seeing_integration(db, resource.id, set(owners.values()))
    return {agent_id for agent_id, owner in owners.items() if owner not in seeing}


def _sees(audience: Audience, user_id: uuid.UUID) -> bool:
    """Whether *audience* (an :meth:`AudienceResolver.entity_audience`) admits
    *user_id*: the rule of ``user_visible_filter`` for a non-administrator."""
    if user_id in audience.user_ids:
        return True
    return audience.everyone and user_id not in audience.excluded_user_ids


async def _agents_a_change_orphans(
    resolver: AudienceResolver,
    after_resolver: AudienceResolver,
    stored: Any,
    proposed: Any,
    refs: Iterable[Reference],
) -> list[Any]:
    """The agents bound to the integration whose owner sees it as *stored*
    and would not as *proposed* (``None`` = deleted): their runs would refuse
    its tools (an orphaned binding, :func:`orphaned_agent_ids`), so a
    revocation names them before it lands. A company-managed or owner-less
    agent orphans nothing."""
    if stored is None:
        return []
    before = await resolver.entity_audience(stored)
    after = await after_resolver.entity_audience(proposed) if proposed is not None else Audience()
    agents: dict[str, Any] = {}
    for ref in refs:
        if ref.kind != PATH_AGENT or REASON_BINDING not in ref.reasons:
            continue
        if getattr(ref.entity, "is_company_managed", False) or getattr(ref.entity, "id", None) is None:
            continue
        owner = normalize_acting_user_id(getattr(ref.entity, "user_id", None))
        if owner is not None and _sees(before, owner) and not _sees(after, owner):
            agents[str(ref.entity.id)] = ref.entity
    return sorted(agents.values(), key=lambda a: (getattr(a, "name", "") or "").lower())


async def orphaned_agents_of_integration(db: AsyncSession, integration: Any) -> list[Any]:
    """The agents bound to *integration* whose owner can no longer see it
    (read by ``get_integration``), in name order."""
    refs = (await find_references(db, RESOURCE_INTEGRATION, [integration.id])).of(integration.id)
    orphaned = await orphaned_agent_ids(db, RESOURCE_INTEGRATION, integration, refs)
    agents = {str(ref.entity.id): ref.entity for ref in refs if ref.kind == PATH_AGENT}
    return sorted((agents[i] for i in orphaned if i in agents), key=lambda a: getattr(a, "name", "") or "")


def _orphan_lends_nothing(ref: Reference) -> bool:
    """An orphaned agent that holds the integration only through its
    bindings lends it to nobody: every run drops the binding."""
    return bool(ref.reasons) and ref.reasons <= _CHAT_SHADOWED_REASONS


def path_notes(kind: str, resource: Any, ref: Reference, usable: bool, *, orphaned: bool = False) -> list[str]:
    notes: list[str] = []
    if orphaned:
        notes.append(NOTE_OWNER_LOST_ACCESS)
    if _chat_uses_own(kind, resource, ref) and not (orphaned and _orphan_lends_nothing(ref)):
        notes.append(NOTE_CHAT_USES_OWN)
    if kind == RESOURCE_INTEGRATION and REASON_PER_USER in ref.reasons:
        notes.append(NOTE_PER_USER_ACTING)
    if kind == RESOURCE_VARIABLE:
        notes.append(NOTE_VARIABLE_RESOLVES_FOR_VIEWERS)
    if kind == RESOURCE_INTEGRATION and not usable and not orphaned:
        notes.append(NOTE_ACCOUNT_NOT_SHARED)
    return notes


# ---------------------------------------------------------------------------
# A resource's exposure (its page)
# ---------------------------------------------------------------------------

_VISIBILITY_MODELS: dict[str, Any] = {
    PATH_AGENT: Agent,
    PATH_FLOW: Flow,
    PATH_KNOWLEDGE_BASE: KnowledgeBase,
    PATH_SKILL: AgentSkill,
    PATH_PROMPT: Prompt,
    PATH_CUSTOM_TOOL: CustomTool,
}


async def _visible_entity_ids(
    db: AsyncSession, refs: list[Reference], viewer_id: uuid.UUID, is_admin: bool
) -> set[str]:
    """Ids of the carriers the viewer may open (three-tier).

    An admin reached this resource by navigation and sees what belongs to it
    (decision 9); anyone else sees a carrier's name only when it is
    visible to them. An assignment is visible when its agent is.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    ids_by_kind: dict[str, set[uuid.UUID]] = {}
    for ref in refs:
        kind = PATH_AGENT if ref.kind == PATH_ASSIGNMENT else ref.kind
        entity = ref.parent if ref.kind == PATH_ASSIGNMENT else ref.entity
        if kind in _VISIBILITY_MODELS and getattr(entity, "id", None) is not None:
            ids_by_kind.setdefault(kind, set()).add(entity.id)
    if is_admin:
        return {str(i) for ids in ids_by_kind.values() for i in ids}
    groups = await get_user_group_ids(db, viewer_id)
    visible: set[str] = set()
    for kind, ids in ids_by_kind.items():
        model = _VISIBILITY_MODELS[kind]
        rows = await db.execute(
            select(model.id).where(model.id.in_(list(ids)), user_visible_filter(model, viewer_id, groups))
        )
        visible |= {str(i) for i in rows.scalars().all()}
    return visible


async def _orphan_summaries(
    db: AsyncSession, agents: list[Any], viewer_id: Any, viewer_is_admin: bool
) -> list[dict[str, Any]]:
    """:func:`_entity_summary` of each agent a change would orphan, named only
    when the viewer may open it; its owner is always named."""
    if not agents:
        return []
    refs = [Reference(PATH_AGENT, agent, {REASON_BINDING}, None) for agent in agents]
    visible = await _visible_entity_ids(db, refs, viewer_id, viewer_is_admin)
    owner_names = await _names(db, [normalize_acting_user_id(getattr(a, "user_id", None)) for a in agents])
    return [_entity_summary(PATH_AGENT, agent, visible, owner_names) for agent in agents]


async def _names(db: AsyncSession, user_ids: Iterable[uuid.UUID | None]) -> dict[str, str]:
    ids = {u for u in user_ids if u is not None}
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(list(ids))))
    return {str(uid): (name or email or "") for uid, name, email in rows.all()}


def _entity_summary(kind: str, entity: Any, visible: set[str], owner_names: dict[str, str]) -> dict[str, Any]:
    if kind == PATH_GENERAL_ASSISTANT:
        return {
            "type": kind,
            "id": None,
            "name": None,
            "visible": True,
            "is_company_managed": True,
            "owner_name": None,
        }
    if kind == PATH_PERSONAL_ASSISTANT:
        return {
            "type": kind,
            "id": str(entity.id),
            "name": getattr(entity, "full_name", None) or getattr(entity, "email", None),
            "visible": True,
            "is_company_managed": False,
            "owner_name": None,
        }
    shown = str(getattr(entity, "id", "")) in visible
    owner = normalize_acting_user_id(getattr(entity, "user_id", None))
    return {
        "type": kind,
        "id": str(entity.id) if shown else None,
        "name": getattr(entity, "name", None) if shown else None,
        "visible": shown,
        "is_company_managed": bool(getattr(entity, "is_company_managed", False)),
        "owner_name": owner_names.get(str(owner)) if owner else None,
    }


def _assignment_summary(ref: Reference, visible: set[str], owner_names: dict[str, str]) -> dict[str, Any]:
    shown = ref.parent is not None and str(ref.parent.id) in visible
    owner = normalize_acting_user_id(getattr(ref.parent, "user_id", None))
    return {
        "type": PATH_ASSIGNMENT,
        "id": str(ref.entity.id) if shown else None,
        "name": ref.entity.name if shown else None,
        "visible": shown,
        "is_company_managed": bool(getattr(ref.entity, "is_company_managed", False)),
        "owner_name": owner_names.get(str(owner)) if owner else None,
    }


def resource_summary(kind: str, resource: Any, names: dict[str, str] | None = None) -> dict[str, Any]:
    owner = normalize_acting_user_id(getattr(resource, "user_id", None))
    return {
        "kind": kind,
        "id": str(resource.id) if getattr(resource, "id", None) else None,
        "name": getattr(resource, "name", None),
        "provider": getattr(resource, "provider", None),
        "auth_type": getattr(resource, "auth_type", None) or getattr(resource, "auth_mode", None),
        "is_company_managed": bool(getattr(resource, "is_company_managed", False)),
        "owner_id": str(owner) if owner else None,
        "owner_name": (names or {}).get(str(owner)) if owner else None,
    }


def _direct_paths(resource: Any) -> list[tuple[str, Audience, bool]]:
    """(path kind, audience, restricted) for each direct share of *resource*."""
    paths: list[tuple[str, Audience, bool]] = []
    if getattr(resource, "is_company_managed", False):
        restricted = bool(getattr(resource, "access_restricted", False))
        paths.append((PATH_COMPANY, Audience(everyone=not restricted), restricted))
    return paths


async def resource_exposure(
    db: AsyncSession,
    kind: str,
    resource: Any,
    *,
    viewer_id: uuid.UUID,
    viewer_is_admin: bool,
) -> dict[str, Any]:
    """Every path through which *resource* reaches people, and why."""
    resolver = AudienceResolver(db)
    secret = describe_secret(kind, resource)
    direct = await direct_audience(resolver, kind, resource, secret)
    references = await find_references(db, kind, [resource.id])
    refs = references.of(resource.id)
    await resolver.prime_agents(_carrier_agents(refs))
    visible = await _visible_entity_ids(db, refs, viewer_id, viewer_is_admin)
    owner_names = await _names(
        db,
        [
            normalize_acting_user_id(getattr(r.parent if r.kind == PATH_ASSIGNMENT else r.entity, "user_id", None))
            for r in refs
            if r.kind not in (PATH_GENERAL_ASSISTANT, PATH_PERSONAL_ASSISTANT)
        ]
        + [safe_uuid(secret["holder_id"]) if secret["holder_id"] else None]
        + [normalize_acting_user_id(getattr(resource, "user_id", None))],
    )

    lends = lends_something(secret)
    shared_usable = lends and secret["account_shared"]
    paths: list[dict[str, Any]] = []
    for path_kind, audience, restricted in _direct_paths(resource):
        paths.append(
            {
                "kind": path_kind,
                "reasons": [],
                "restricted": restricted,
                "entity": None,
                "parent": None,
                "usable": shared_usable,
                "notes": [] if shared_usable or not lends else [NOTE_ACCOUNT_NOT_SHARED],
                "audience": await resolver.serialize(audience),
            }
        )
    for group_id in sorted(_uuids(getattr(resource, "shared_group_ids", None)), key=str):
        members = await resolver.members(group_id)
        paths.append(
            {
                "kind": PATH_GROUP,
                "reasons": [],
                "restricted": False,
                "entity": None,
                "parent": None,
                "usable": shared_usable,
                "notes": [] if shared_usable or not lends else [NOTE_ACCOUNT_NOT_SHARED],
                "audience": await resolver.serialize(Audience(user_ids=set(members), group_ids={group_id})),
            }
        )
    granted = _uuids(getattr(resource, "shared_user_ids", None))
    if granted:
        paths.append(
            {
                "kind": PATH_PEOPLE,
                "reasons": [],
                "restricted": False,
                "entity": None,
                "parent": None,
                "usable": shared_usable,
                "notes": [] if shared_usable or not lends else [NOTE_ACCOUNT_NOT_SHARED],
                "audience": await resolver.serialize(Audience(user_ids=set(granted), named_user_ids=set(granted))),
            }
        )

    reached = direct if lends else Audience()
    orphaned_ids = await orphaned_agent_ids(db, kind, resource, refs)
    orphaned_entities: list[dict[str, Any]] = []
    for ref in refs:
        audience = await resolver.carrier_audience(ref.kind, ref.entity, ref.parent)
        orphaned = ref.kind == PATH_AGENT and str(getattr(ref.entity, "id", "")) in orphaned_ids
        if orphaned and _orphan_lends_nothing(ref):
            # Its runs drop the binding: nobody acts with the credential
            # through it, and its owner is not counted in the headline.
            lent = Audience()
            usable = False
        else:
            lent = await lent_audience(resolver, kind, resource, ref, secret)
            usable = await path_usable(db, kind, resource, ref, secret)
            if _per_user(kind, resource) and ref.kind == PATH_AGENT:
                # Its chats act with each person's own account.
                usable = usable and not lent.is_empty
        reached = reached.merge(lent)
        if orphaned:
            orphaned_entities.append(_entity_summary(ref.kind, ref.entity, visible, owner_names))
        paths.append(
            {
                "kind": ref.kind,
                "reasons": sorted(ref.reasons),
                "restricted": False,
                "entity": _assignment_summary(ref, visible, owner_names)
                if ref.kind == PATH_ASSIGNMENT
                else _entity_summary(ref.kind, ref.entity, visible, owner_names),
                "parent": _entity_summary(PATH_AGENT, ref.parent, visible, owner_names)
                if ref.kind == PATH_ASSIGNMENT
                else None,
                "usable": usable,
                "orphaned": orphaned,
                "notes": path_notes(kind, resource, ref, usable, orphaned=orphaned) if lends else [],
                "audience": await resolver.serialize(audience),
                # Who acts with what it stores through this path: the
                # headline's share of it (an agent's chat audience left out
                # where its chats use their own).
                "lent": await resolver.serialize(lent),
                "beyond_direct": await resolver.serialize(audience.beyond(direct)),
            }
        )

    holder = secret["holder_id"]
    return {
        "resource": resource_summary(kind, resource, owner_names),
        "secret": {**secret, "holder_name": owner_names.get(holder) if holder else None},
        "direct": await resolver.serialize(direct),
        "paths": paths,
        "reached": await resolver.serialize(reached),
        # Agents bound to the integration whose owner can no longer see it.
        "orphaned_agents": orphaned_entities,
        "truncated": references.truncated,
    }


async def exposure_summaries(
    db: AsyncSession, kind: str, resources: Iterable[Any]
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Per resource: how many ways it is shared, by path kind (the list cards),
    and whether a lookup was cut."""
    rows = list(resources)
    references = await find_references(db, kind, [r.id for r in rows])
    summaries: dict[str, dict[str, Any]] = {}
    for resource in rows:
        counts: dict[str, int] = {}
        if getattr(resource, "is_company_managed", False):
            counts[PATH_COMPANY] = 1
        groups = len(_uuids(getattr(resource, "shared_group_ids", None)))
        if groups:
            counts[PATH_GROUP] = groups
        people = len(_uuids(getattr(resource, "shared_user_ids", None)))
        if people:
            counts[PATH_PEOPLE] = people
        refs = references.of(resource.id)
        for ref in refs:
            counts[ref.kind] = counts.get(ref.kind, 0) + 1
        public = any(
            getattr(r.parent if r.kind == PATH_ASSIGNMENT else r.entity, "web_enabled", False)
            for r in refs
            if r.kind in (PATH_AGENT, PATH_ASSIGNMENT)
        )
        secret = describe_secret(kind, resource)
        summaries[str(resource.id)] = {
            "id": str(resource.id),
            "kind": kind,
            "secret_kind": secret["kind"],
            "is_company": secret["is_company"],
            "account_shared": secret["account_shared"],
            "ways": sum(counts.values()),
            "counts": counts,
            "public_link": public,
        }
    return summaries, references.truncated


# ---------------------------------------------------------------------------
# Preview: what a change lends to new people, and takes from others
# ---------------------------------------------------------------------------


#: What a NEW entity's preview may also say about it: its name, and for a
#: resource being created what it will hold (``holds_secret``: the form has a
#: key, headers or a token typed in), since nothing is stored yet.
_CREATION_HINTS = ("name", "provider", "auth_type", "auth_mode", "is_secret", "trigger_type")

#: The column :func:`describe_secret` reads for a resource that will hold a
#: secret once created.
_SECRET_MARKER_COLUMN = {
    RESOURCE_INTEGRATION: "api_key_enc",
    RESOURCE_MCP_SERVER: "headers_enc",
    RESOURCE_CHANNEL_BOT: "api_key_enc",
}
_PROPOSED_VALUE = "proposed"
#: The id a preview gives an entity that does not exist yet.
_NEW = "new"


def proposed_copy(
    entity: Any,
    change: dict[str, Any],
    *,
    entity_type: str,
    actor_id: Any = None,
    parent: Any = None,
) -> SimpleNamespace:
    """A copy of *entity* (or of a new one) with *change* applied.

    Only the fields :data:`_PROPOSABLE` lists for *entity_type* are applied;
    ``new_owner_id`` moves the owner. A new entity belongs to *actor_id* (an
    assignment to *parent*'s agent), and takes the :data:`_CREATION_HINTS`.
    Nothing is written.
    """
    values = {name: getattr(entity, name, None) for name in _COPIED_FIELDS}
    if entity is None:
        values["user_id"] = normalize_acting_user_id(actor_id)
        if parent is not None:
            values["agent_id"] = getattr(parent, "id", None)
        for name in _CREATION_HINTS:
            if name in change:
                values[name] = change[name]
        marker = _SECRET_MARKER_COLUMN.get(entity_type)
        if change.get("holds_secret") and marker:
            if entity_type == RESOURCE_INTEGRATION and values.get("auth_type") == "oauth":
                values["access_token_enc"] = _PROPOSED_VALUE
            else:
                values[marker] = _PROPOSED_VALUE
    for name in _PROPOSABLE.get(entity_type, ()):
        if name in change and name not in _CHANGE_ONLY_FIELDS:
            values[name] = change[name]
    removed_channel = change.get("remove_channel")
    if entity_type == ENTITY_AGENT and isinstance(removed_channel, str) and isinstance(values["channel_config"], dict):
        values["channel_config"] = {k: v for k, v in values["channel_config"].items() if k != removed_channel}
    if change.get("new_owner_id"):
        values["user_id"] = safe_uuid(str(change["new_owner_id"]))
    if (
        entity_type == ENTITY_FLOW
        and values.get("status") == "active"
        and not values.get("webhook_secret")
        and isinstance(values.get("graph"), dict)
    ):
        from app.services.flow_runner import find_trigger_nodes

        # Activation mints the webhook secret of a flow with a webhook trigger.
        if find_trigger_nodes(values["graph"], "trigger.webhook"):
            values["webhook_secret"] = _PROPOSED_VALUE
    for flag in ("is_company_managed", "access_restricted", "web_enabled"):
        values[flag] = bool(values.get(flag))
    return SimpleNamespace(**values)


def _settle_integration_consent(
    before: Any, after: SimpleNamespace, change: dict[str, Any], *, actor_id: Any, actor_is_admin: bool
) -> list[str]:
    """Apply the REST routes' consent rule to a proposed integration copy.

    The same calls ``PUT /integrations/{id}`` (and, for a new one, ``POST
    /integrations``) make, on the copy: the request's explicit
    ``keep_oauth_credentials_on_share`` (``None`` when it makes no choice),
    whether it changes sharing (a creation always does), and whether the actor
    may consent (``may_consent_to_share``); then the owner transfer's record of
    whose token the row keeps. Returns notes (a refused explicit share).
    """
    notes: list[str] = []
    choice = change.get("keep_oauth_credentials_on_share")
    sharing_changed = before is None or any(name in change for name in _SHARING_FIELDS)
    may_consent = may_consent_to_share(before, actor_id, is_admin=actor_is_admin) if before is not None else True
    try:
        apply_oauth_share_consent(
            after,
            share_credentials=requested_share_choice(
                None if choice is None else bool(choice),
                before if before is not None else after,
                sharing_changed=sharing_changed,
            ),
            sharing_changed=sharing_changed,
            may_consent=may_consent,
            implicit_consent=False,
        )
    except ShareConsentRefusedError:
        notes.append("share_consent_refused")
    if change.get("new_owner_id") and before is not None:
        previous = normalize_acting_user_id(getattr(before, "user_id", None))
        if normalize_acting_user_id(after.user_id) != previous:
            record_token_owner_on_transfer(after, previous)
    return notes


@dataclass
class _World:
    """One state of the platform around the changed entity: stored or proposed."""

    #: agent id -> the agent's state, for an assignment's audience.
    agents: dict[str, Any] = field(default_factory=dict)
    #: resource (kind, id) -> its state, ``None`` when deleted.
    resources: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: resource (kind, id) -> references the changed carrier adds or drops.
    extra_refs: dict[tuple[str, str], list[Reference]] = field(default_factory=dict)
    #: carriers whose references are recomputed (dropped from the stored list).
    replaced: set[tuple[str, str]] = field(default_factory=set)
    #: resource (kind, id) -> the stored references of the carriers the
    #: change recomputes, counted whether or not a cut lookup reached them.
    stored_refs: dict[tuple[str, str], list[Reference]] = field(default_factory=dict)
    #: agents whose assignments no longer exist in this world.
    removed_agents: set[str] = field(default_factory=set)
    #: company -> private: the agents of anyone but this owner lose the
    #: resource (``handle_company_managed_toggle`` and the integration route
    #: clean their bindings).
    cleaned_agent_owner: dict[tuple[str, str], uuid.UUID | None] = field(default_factory=dict)


async def _reached(
    db: AsyncSession,
    resolver: AudienceResolver,
    kind: str,
    resource_id: str,
    stored: Any,
    stored_refs: list[Reference],
    world: _World | None,
) -> Audience:
    """Who can act with the resource in *world* (``None`` = as stored)."""
    key = (kind, resource_id)
    resource = world.resources.get(key, stored) if world is not None else stored
    if resource is None:
        return Audience()
    secret = describe_secret(kind, resource)
    if not lends_something(secret):
        return Audience()
    reached = await direct_audience(resolver, kind, resource, secret)
    refs = list(stored_refs)
    if world is not None:
        cleaning = key in world.cleaned_agent_owner
        cleaned_owner = world.cleaned_agent_owner.get(key)
        kept: list[Reference] = []
        for ref in refs:
            if ref.entity_key in world.replaced:
                continue
            parent_key = str(getattr(ref.parent, "id", "")) if ref.parent is not None else None
            if ref.kind == PATH_ASSIGNMENT and parent_key in world.removed_agents:
                continue
            # Company -> private removes the resource from other owners' agents,
            # and an integration or MCP server from every company-managed agent
            # too (#11866: such an agent binds company-managed ones only).
            if (
                cleaning
                and ref.kind == PATH_AGENT
                and (
                    normalize_acting_user_id(getattr(ref.entity, "user_id", None)) != cleaned_owner
                    or (
                        kind in (RESOURCE_INTEGRATION, RESOURCE_MCP_SERVER)
                        and bool(getattr(ref.entity, "is_company_managed", False))
                    )
                )
            ):
                continue
            if ref.kind == PATH_ASSIGNMENT and parent_key in world.agents:
                ref = Reference(ref.kind, ref.entity, set(ref.reasons), world.agents[parent_key])
            kept.append(ref)
        refs = kept + world.extra_refs.get(key, [])
    for ref in refs:
        reached = reached.merge(await lent_audience(resolver, kind, resource, ref, secret))
    return reached


def _opt_outs(kind: str, resource: Any, entity_type: str, actor_id: Any, actor_is_admin: bool) -> list[str]:
    options: list[str] = []
    if kind in ATTACHED_ONLY_KINDS:
        return options
    secret = describe_secret(kind, resource)
    if (
        kind == RESOURCE_INTEGRATION
        and secret["kind"] == HOLDS_OAUTH_ACCOUNT
        and may_consent_to_share(resource, actor_id, is_admin=actor_is_admin)
    ):
        options.append(OPT_OUT_WITHHOLD_ACCOUNT)
    # An assignment's runs act as one person, whose own integration a
    # per-user binding would pick: only an agent's audience is spared by it.
    if kind == RESOURCE_INTEGRATION and entity_type == ENTITY_AGENT:
        options.append(OPT_OUT_PER_USER)
    if not secret["is_company"]:
        options.append(OPT_OUT_COMPANY_RESOURCE)
    return options


async def _load_resources(db: AsyncSession, kind: str, ids: Iterable[str]) -> dict[str, Any]:
    parsed = [uuid.UUID(i) for i in _canonical(ids)]
    if not parsed:
        return {}
    model = RESOURCE_MODELS[kind]
    rows = (await db.execute(select(model).where(model.id.in_(parsed)).limit(MAX_ENTITIES_PER_KIND))).scalars().all()
    return {str(row.id): row for row in rows}


async def _ids_writer_cannot_see(
    db: AsyncSession, actor_id: Any, *, is_admin: bool, kind: str, ids: Iterable[str]
) -> set[str]:
    """The ids a change ADDS that its writer may not set, so a preview never
    describes a resource its caller cannot see (every kind).

    Integrations use the saves' own rule (``integration_ids_writer_cannot_see``:
    admins may set any); every other resource the three-tier filter, which an
    admin also passes - except a variable, which an admin cannot open on its
    own detail route either (``api/v1/resource_exposure._load_resource``). An id naming nothing is
    dropped as well: it describes nothing.
    """
    wanted = _canonical(ids)
    if not wanted:
        return set()
    if kind == RESOURCE_INTEGRATION:
        from app.services.integration_credentials import integration_ids_writer_cannot_see

        return await integration_ids_writer_cannot_see(db, actor_id, is_admin=is_admin, ids=wanted, stored=())
    if is_admin and kind != RESOURCE_VARIABLE:
        return set()
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter

    actor = normalize_acting_user_id(actor_id)
    if actor is None:
        return wanted
    model = RESOURCE_MODELS[kind]
    groups = await get_user_group_ids(db, actor)
    rows = await db.execute(
        select(model.id).where(
            model.id.in_([uuid.UUID(i) for i in wanted]),
            user_visible_filter(model, actor, groups),
        )
    )
    return wanted - {str(i) for i in rows.scalars().all()}


async def _agent_assignments(db: AsyncSession, agent_id: Any) -> tuple[list[Assignment], bool]:
    """The agent's assignments, and whether the lookup was cut."""
    parsed = safe_uuid(str(agent_id)) if agent_id else None
    if parsed is None:
        return [], False
    rows = list(
        (await db.execute(select(Assignment).where(Assignment.agent_id == parsed).limit(MAX_ENTITIES_PER_KIND + 1)))
        .scalars()
        .all()
    )
    return rows[:MAX_ENTITIES_PER_KIND], len(rows) > MAX_ENTITIES_PER_KIND


async def _kb_sync_map(db: AsyncSession, kb_ids: Iterable[Any]) -> dict[str, list[str]]:
    """Knowledge base id -> the integrations its sync sources hold."""
    ids = sorted(_uuids(kb_ids), key=str)
    if not ids:
        return {}
    rows = await db.execute(
        select(KBSyncSource.knowledge_base_id, KBSyncSource.integration_id).where(
            KBSyncSource.knowledge_base_id.in_(ids), KBSyncSource.integration_id.isnot(None)
        )
    )
    out: dict[str, list[str]] = {}
    for kb_id, integration_id in rows.all():
        out.setdefault(str(kb_id), []).append(str(integration_id))
    return out


async def _affect_carriers(
    db: AsyncSession, affect: Callable[[str, Iterable[str]], None], carriers: Iterable[tuple[str, Any, Any]]
) -> bool:
    """Mark as affected everything *carriers* ``(path, entity, parent)`` hold,
    and for an agent everything its assignments hold, when a change moves
    their audience without touching them; whether a lookup was cut."""
    carriers = list(carriers)
    sync = await _kb_sync_map(db, [getattr(e, "id", None) for path, e, _ in carriers if path == PATH_KNOWLEDGE_BASE])
    attached = await attached_holdings(
        db, [e for path, e, _ in carriers if path in (PATH_AGENT, PATH_FLOW, PATH_GENERAL_ASSISTANT)]
    )
    agents: dict[uuid.UUID, Any] = {}
    pairs: list[tuple[Any, Any]] = []
    for path, entity, parent in carriers:
        if path == PATH_AGENT and getattr(entity, "id", None) is not None:
            agents[entity.id] = entity
        elif path == PATH_ASSIGNMENT:
            pairs.append((entity, parent))
        else:
            held = held_resources(
                path,
                entity,
                sync_source_integration_ids=sync.get(str(getattr(entity, "id", ""))),
                attached=attached.get(id(entity)),
            )
            for kind, ids in held.items():
                affect(kind, ids)
    for agent in agents.values():
        for kind, ids in held_resources(PATH_AGENT, agent, attached=attached.get(id(agent))).items():
            affect(kind, ids)
    truncated = False
    if agents:
        assignments = (
            (
                await db.execute(
                    select(Assignment).where(Assignment.agent_id.in_(list(agents))).limit(MAX_ENTITIES_PER_KIND + 1)
                )
            )
            .scalars()
            .all()
        )
        truncated = len(assignments) > MAX_ENTITIES_PER_KIND
        pairs.extend((assignment, agents[assignment.agent_id]) for assignment in assignments[:MAX_ENTITIES_PER_KIND])
    picks = await per_user_integrations(db, pairs)
    for assignment, _parent in pairs:
        held = held_resources(PATH_ASSIGNMENT, assignment, per_user_integration_ids=picks.get(id(assignment)))
        for kind, ids in held.items():
            affect(kind, ids)
    return truncated or picks.truncated


#: What can be shared with a group: (resource kind or carrier path, model).
#: A custom tool is both, a resource and a carrier of the variables it names.
_GROUP_SHAREABLE: tuple[tuple[str, Any], ...] = (
    (RESOURCE_INTEGRATION, Integration),
    (RESOURCE_MCP_SERVER, MCPServer),
    (RESOURCE_CUSTOM_TOOL, CustomTool),
    (RESOURCE_CHANNEL_BOT, ChannelBot),
    (RESOURCE_VARIABLE, Variable),
    (PATH_AGENT, Agent),
    (PATH_FLOW, Flow),
    (PATH_KNOWLEDGE_BASE, KnowledgeBase),
    (PATH_SKILL, AgentSkill),
    (PATH_PROMPT, Prompt),
)


async def _shared_with_group(
    db: AsyncSession, group_id: uuid.UUID
) -> tuple[list[tuple[str, str]], list[tuple[str, Any, Any]], bool]:
    """The resources ``(kind, id)`` and the carriers ``(path, entity, None)``
    shared with *group_id*, and whether a lookup was cut."""
    resources: list[tuple[str, str]] = []
    carriers: list[tuple[str, Any, Any]] = []
    truncated = False
    for kind, model in _GROUP_SHAREABLE:
        if not hasattr(model, "shared_group_ids"):
            continue
        rows = (
            (
                await db.execute(
                    select(model)
                    .where(cast(model.shared_group_ids, Text).ilike(f"%{group_id}%"))
                    .limit(MAX_ENTITIES_PER_KIND + 1)
                )
            )
            .scalars()
            .all()
        )
        truncated |= len(rows) > MAX_ENTITIES_PER_KIND
        for row in rows[:MAX_ENTITIES_PER_KIND]:
            if group_id not in _uuids(getattr(row, "shared_group_ids", None)):
                continue
            if kind in RESOURCE_KINDS and kind not in ATTACHED_ONLY_KINDS:
                resources.append((kind, str(row.id)))
            path = PATH_CUSTOM_TOOL if kind == RESOURCE_CUSTOM_TOOL else kind
            if path in CARRIER_PATHS:
                carriers.append((path, row, None))
    return resources, carriers, truncated


_ATTACHMENT_COLUMN = {
    PATH_KNOWLEDGE_BASE: "knowledge_base_ids",
    PATH_SKILL: "skill_ids",
    PATH_PROMPT: "prompt_ids",
}


async def _attachers(db: AsyncSession, path: str, carrier_id: uuid.UUID) -> tuple[list[tuple[str, Any]], bool]:
    """The agents, flows, and the General Assistant for a knowledge base,
    attaching the knowledge base, skill or prompt *carrier_id* (``(path,
    entity)``), and whether a lookup was cut."""
    column = _ATTACHMENT_COLUMN[path]
    key = str(carrier_id)
    out: list[tuple[str, Any]] = []
    truncated = False
    for attacher_path, model, scanned in ((PATH_AGENT, Agent, getattr(Agent, column)), (PATH_FLOW, Flow, Flow.graph)):
        if attacher_path == PATH_FLOW and column not in _FLOW_ATTACHMENT_NODES.values():
            continue
        rows = (
            (await db.execute(select(model).where(_contains_any((scanned,), [key])).limit(MAX_ENTITIES_PER_KIND + 1)))
            .scalars()
            .all()
        )
        truncated |= len(rows) > MAX_ENTITIES_PER_KIND
        out.extend((attacher_path, row) for row in rows[:MAX_ENTITIES_PER_KIND] if key in _attachment_ids(row, column))
    if path == PATH_KNOWLEDGE_BASE:
        ga = (await db.execute(select(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
        if ga is not None and key in _attachment_ids(ga, column):
            out.append((PATH_GENERAL_ASSISTANT, ga))
    return out, truncated


async def _proposed_a2a_exposure(db: AsyncSession, stored: Any, proposed: Any) -> bool | None:
    """Whether *proposed* will be exposed over A2A once saved, when its
    bindings decide it: an exposure follows an inbound A2A binding
    (``app.a2a.hooks``). ``None`` leaves the live exposure the answer."""
    from app.a2a.providers import bound_integration_ids, inbound_integration_ids

    async def inbound(agent: Any) -> set[uuid.UUID]:
        if agent is None or not bound_integration_ids(agent):
            return set()
        return set(await db.run_sync(lambda session: inbound_integration_ids(session, agent)))

    after = await inbound(proposed)
    if stored is not None and await inbound(stored) == after:
        return None
    return bool(after) and normalize_acting_user_id(getattr(proposed, "user_id", None)) is not None


async def preview_change(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    actor_is_admin: bool,
    entity_type: str,
    entity: Any,
    change: dict[str, Any],
    parent: Any = None,
) -> dict[str, Any]:
    """The resources a proposed change lends to people who could not use them
    before, and takes away from people who could, with whose they are and
    through what.

    *entity* is the stored row (``None`` when creating one; *parent* is an
    assignment's agent; a group, or the platform settings for the platform
    MCP endpoint). *change* holds only what the change sets: the fields
    :data:`_PROPOSABLE` lists, ``new_owner_id`` for an owner transfer, and
    ``delete`` for a deletion. Each resource is compared as a whole: who can
    act with it through every path before and after, so a person it already
    reached another way is never announced again. Nothing is written.
    """
    resolver = AudienceResolver(db)
    # Answers "after" when the change moves an audience the resolver reads
    # itself: a channel bot, a group's members, the platform MCP key.
    after_resolver = resolver
    deleting = bool(change.get("delete"))
    after = (
        None if deleting else proposed_copy(entity, change, entity_type=entity_type, actor_id=actor_id, parent=parent)
    )
    world = _World()
    affected: dict[str, set[str]] = {}
    notes_by_resource: dict[tuple[str, str], list[str]] = {}
    via: dict[str, Any] | None = None
    actor = normalize_acting_user_id(actor_id)

    def affect(kind: str, ids: Iterable[str]) -> None:
        affected.setdefault(kind, set()).update(_canonical(ids))

    async def lend_through(
        path: str,
        carrier: Any,
        carrier_parent: Any,
        held_before: dict[str, dict[str, set[str]]],
        held_after: dict[str, dict[str, set[str]]],
    ) -> None:
        for kind in RESOURCE_KINDS:
            before_ids = set(held_before.get(kind, {}))
            # What the change ADDS is described only when its writer may set
            # it; what the entity already holds is described as before.
            hidden = await _ids_writer_cannot_see(
                db,
                actor_id,
                is_admin=actor_is_admin,
                kind=kind,
                ids=set(held_after.get(kind, {})) - before_ids,
            )
            affect(kind, before_ids)
            affect(kind, set(held_after.get(kind, {})) - hidden)
            for resource_id, reasons in held_after.get(kind, {}).items():
                if resource_id in hidden:
                    continue
                world.extra_refs.setdefault((kind, resource_id), []).append(
                    Reference(path, carrier, set(reasons), carrier_parent)
                )

    def keep_stored(path: str, carrier: Any, carrier_parent: Any, held: dict[str, dict[str, set[str]]]) -> None:
        if carrier is None:
            return
        for kind, ids in held.items():
            for resource_id, reasons in ids.items():
                world.stored_refs.setdefault((kind, resource_id), []).append(
                    Reference(path, carrier, set(reasons), carrier_parent)
                )

    cut = False
    if entity_type in RESOURCE_KINDS and entity_type not in ATTACHED_ONLY_KINDS:
        resource_id = str(getattr(entity, "id", "") or "") or _NEW
        key = (entity_type, resource_id)
        if after is not None and entity_type == RESOURCE_INTEGRATION:
            notes_by_resource[key] = _settle_integration_consent(
                entity, after, change, actor_id=actor_id, actor_is_admin=actor_is_admin
            )
        world.resources[key] = after
        if (
            entity is not None
            and after is not None
            and getattr(entity, "is_company_managed", False)
            and not after.is_company_managed
        ):
            world.cleaned_agent_owner[key] = normalize_acting_user_id(getattr(after, "user_id", None))
        affected.setdefault(entity_type, set()).add(resource_id)
        if (
            entity_type == RESOURCE_CHANNEL_BOT
            and entity is not None
            and after is not None
            and "allow_anonymous" in change
        ):
            # Every agent it serves answers people without an account, or
            # stops: what they carry is lent to them, or taken back.
            after_resolver = AudienceResolver(db, bots={entity.id: after})
            bot_refs = (await find_references(db, RESOURCE_CHANNEL_BOT, [entity.id])).of(entity.id)
            cut |= await _affect_carriers(db, affect, [(r.kind, r.entity, r.parent) for r in bot_refs])
            via = {"type": entity_type, "id": str(entity.id), "name": getattr(entity, "name", None)}

    moved_members: set[uuid.UUID] = set()
    if entity_type == ENTITY_GROUP and entity is not None:
        members = set(await resolver.members(entity.id))
        moved_members = (_uuids(change.get("add_user_ids")) - members) | (
            _uuids(change.get("remove_user_ids")) & members
        )
        members |= _uuids(change.get("add_user_ids"))
        members -= _uuids(change.get("remove_user_ids"))
        after_resolver = AudienceResolver(db, members={entity.id: members})
        resources, carriers, group_cut = await _shared_with_group(db, entity.id)
        for kind, resource_id in resources:
            affect(kind, [resource_id])
        cut |= group_cut | await _affect_carriers(db, affect, carriers)
        via = {"type": entity_type, "id": str(entity.id), "name": getattr(entity, "name", None)}

    if entity_type == ENTITY_PLATFORM_MCP:
        exposed_before = await resolver._mcp_exposed()
        listed = (
            change.get("mcp_exposed_agent_ids")
            if "mcp_exposed_agent_ids" in change
            else getattr(entity, "mcp_exposed_agent_ids", None)
        )
        has_key = (
            bool(change["has_mcp_key"])
            if "has_mcp_key" in change
            else bool(getattr(entity, "mcp_global_api_key_hash", None))
        )
        exposed_after = {str(p) for p in _uuids(listed)} if has_key else set()
        after_resolver = AudienceResolver(db, mcp_agent_ids=exposed_after)
        moved = sorted(exposed_before ^ exposed_after)
        agents = (
            (await db.execute(select(Agent).where(Agent.id.in_([uuid.UUID(i) for i in moved])))).scalars().all()
            if moved
            else []
        )
        cut |= await _affect_carriers(db, affect, [(PATH_AGENT, agent, None) for agent in agents])
        via = {"type": entity_type, "id": None, "name": None}

    carrier_path = _ENTITY_PATH.get(entity_type)
    if carrier_path is not None:
        before_sync = after_sync = None
        if carrier_path == PATH_KNOWLEDGE_BASE:
            before_sync = await _kb_sync_integration_ids(db, getattr(entity, "id", None))
            after_sync = (
                _as_list(change["sync_source_integration_ids"])
                if "sync_source_integration_ids" in change
                else before_sync
            )
        before_picks = after_picks = PerUserPicks()
        if carrier_path == PATH_ASSIGNMENT:
            before_picks = await per_user_integrations(db, [(entity, parent)])
            after_picks = await per_user_integrations(db, [(after, parent)])
            cut |= before_picks.truncated or after_picks.truncated
        if carrier_path == PATH_AGENT and after is not None:
            after.a2a_exposed = await _proposed_a2a_exposure(db, entity, after)
        attached = (
            await attached_holdings(db, [entity, after])
            if carrier_path in (PATH_AGENT, PATH_FLOW, PATH_GENERAL_ASSISTANT)
            else {}
        )
        held_before = held_resources(
            carrier_path,
            entity,
            sync_source_integration_ids=before_sync,
            per_user_integration_ids=before_picks.get(id(entity)),
            attached=attached.get(id(entity)),
        )
        held_after = held_resources(
            carrier_path,
            after,
            sync_source_integration_ids=after_sync,
            per_user_integration_ids=after_picks.get(id(after)),
            attached=attached.get(id(after)),
        )
        carrier_id = str(getattr(entity, "id", "") or "") or _NEW
        world.replaced.add((carrier_path, carrier_id))
        carrier_parent = parent if carrier_path == PATH_ASSIGNMENT else None
        keep_stored(carrier_path, entity, carrier_parent, held_before)
        await lend_through(carrier_path, after, carrier_parent, held_before, held_after)
        if carrier_path in _ATTACHED_CARRIERS and entity is not None:
            # The agents attaching it carry what it will hold into their runs.
            override = (
                {"kb_sync": {str(entity.id): [str(i) for i in _as_list(after_sync)] if after is not None else []}}
                if carrier_path == PATH_KNOWLEDGE_BASE
                else {("prompts" if carrier_path == PATH_PROMPT else "skills"): {str(entity.id): after}}
            )
            attachers, attachers_cut = await _attachers(db, carrier_path, entity.id)
            cut |= attachers_cut
            for attacher_path, attacher in attachers:
                stored = await attached_holdings(db, [attacher])
                proposed = await attached_holdings(db, [attacher], **override)
                if stored.get(id(attacher)) == proposed.get(id(attacher)):
                    continue
                world.replaced.add((attacher_path, str(attacher.id)))
                attacher_held = held_resources(attacher_path, attacher, attached=stored.get(id(attacher)))
                keep_stored(attacher_path, attacher, None, attacher_held)
                await lend_through(
                    attacher_path,
                    attacher,
                    None,
                    attacher_held,
                    held_resources(attacher_path, attacher, attached=proposed.get(id(attacher))),
                )
        if carrier_path == PATH_AGENT:
            agent_id = str(getattr(entity, "id", "") or "")
            if agent_id:
                if after is None:
                    world.removed_agents.add(agent_id)
                else:
                    world.agents[agent_id] = after
                # The agent's assignments reach its audience: what they hold is
                # lent (or taken back) by a change of the agent's audience too,
                # and what they resolve per user follows its bindings and owner.
                assignments, assignments_cut = await _agent_assignments(db, agent_id)
                stored_picks = await per_user_integrations(db, [(a, entity) for a in assignments])
                proposed_picks = (
                    await per_user_integrations(db, [(a, after) for a in assignments])
                    if after is not None
                    else PerUserPicks()
                )
                cut |= assignments_cut or stored_picks.truncated or proposed_picks.truncated
                for assignment in assignments:
                    stored_held = held_resources(
                        PATH_ASSIGNMENT, assignment, per_user_integration_ids=stored_picks.get(id(assignment))
                    )
                    for kind, ids in stored_held.items():
                        affect(kind, ids)
                    keep_stored(PATH_ASSIGNMENT, assignment, entity, stored_held)
                    if after is None or stored_picks.get(id(assignment)) == proposed_picks.get(id(assignment)):
                        continue
                    world.replaced.add((PATH_ASSIGNMENT, str(assignment.id)))
                    await lend_through(
                        PATH_ASSIGNMENT,
                        assignment,
                        after,
                        stored_held,
                        held_resources(
                            PATH_ASSIGNMENT, assignment, per_user_integration_ids=proposed_picks.get(id(assignment))
                        ),
                    )
        name = getattr(after or entity, "name", None)
        via = {
            "type": entity_type,
            "id": str(entity.id) if getattr(entity, "id", None) else None,
            "name": name,
        }

    items: list[dict[str, Any]] = []
    truncated = cut
    for kind in RESOURCE_KINDS:
        ids = affected.get(kind, set())
        if not ids:
            continue
        stored_rows: dict[str, Any] = await _load_resources(db, kind, [i for i in ids if i != _NEW])
        if _NEW in ids:
            stored_rows[_NEW] = None
        references = await find_references(db, kind, [i for i in stored_rows if i != _NEW])
        for resource_id in stored_rows:
            for ref in world.stored_refs.get((kind, resource_id), []):
                references.add_stored(resource_id, ref)
        # Every carrier a cut lookup leaves out reaches the same people before
        # and after the change (the ones it recomputes were added above), so
        # leaving it out can only name someone who already had access, never
        # hide one. Only a change to the resource itself, or to who carriers
        # reach, moves what the left-out carriers lend.
        if references.truncated and (
            after_resolver is not resolver
            or any((kind, resource_id) in world.resources for resource_id in stored_rows if resource_id != _NEW)
        ):
            truncated = True
        added = [ref for (ref_kind, _), refs in world.extra_refs.items() if ref_kind == kind for ref in refs]
        carrier_agents = [*_carrier_agents(references.all() + added), *world.agents.values()]
        await resolver.prime_agents(carrier_agents)
        if after_resolver is not resolver:
            await after_resolver.prime_agents(carrier_agents)
        names = await _names(
            db,
            [normalize_acting_user_id(getattr(r, "user_id", None)) for r in stored_rows.values() if r]
            + [
                normalize_acting_user_id(getattr(world.resources.get((kind, rid)), "user_id", None))
                for rid in stored_rows
            ],
        )
        for resource_id, stored in sorted(
            stored_rows.items(), key=lambda kv: (getattr(kv[1], "name", "") or "").lower()
        ):
            after_resource = world.resources.get((kind, resource_id), stored)
            subject = after_resource if after_resource is not None else stored
            if subject is None:
                continue
            refs = references.of(resource_id)
            before_reached = (
                await _reached(db, resolver, kind, resource_id, stored, refs, None)
                if stored is not None
                else Audience()
            )
            after_reached = await _reached(db, after_resolver, kind, resource_id, stored, refs, world)
            secret = describe_secret(kind, subject)
            holder = safe_uuid(secret["holder_id"]) if secret["holder_id"] else None
            gained = after_reached.beyond(before_reached).without([holder, actor])
            lost = before_reached.beyond(after_reached).without([holder, actor])
            orphans = (
                await _agents_a_change_orphans(
                    resolver, after_resolver, stored, world.resources.get((kind, resource_id), stored), refs
                )
                if kind == RESOURCE_INTEGRATION
                else []
            )
            if gained.is_empty and lost.is_empty and not orphans:
                continue
            gained.named_user_ids |= gained.user_ids & moved_members
            lost.named_user_ids |= lost.user_ids & moved_members
            holder_names = await _names(db, [holder])
            items.append(
                {
                    "resource": resource_summary(kind, subject, names),
                    "secret": {
                        **secret,
                        "holder_name": holder_names.get(str(holder)) if holder else None,
                        "is_yours": holder is not None and holder == actor,
                    },
                    "via": via if (kind, resource_id) not in world.resources else None,
                    "gained": await after_resolver.serialize(gained),
                    "lost": await resolver.serialize(lost),
                    "notes": notes_by_resource.get((kind, resource_id), []),
                    "opt_outs": _opt_outs(kind, subject, entity_type, actor_id, actor_is_admin),
                    # Bound agents whose owner the change takes this
                    # integration from: the caller may unbind them with it.
                    "orphaned_agents": await _orphan_summaries(db, orphans, actor_id, actor_is_admin),
                }
            )
    return {
        "items": items,
        "requires_confirmation": bool(items) or truncated,
        "truncated": truncated,
    }
