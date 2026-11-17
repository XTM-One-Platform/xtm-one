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

"""File the conversation into a workspace by asking the agent (spec 032 FR-027b).

Filing one's own conversation is a person's right, not an agent capability
someone must configure: this suite is offered on every turn acting for an
identified person, whatever the agent. It holds three tools -
``list_workspaces``, ``create_workspace`` and ``file_this_conversation`` - and
their handlers are the workspace tools' own (``builtin_tools.workspaces``), so
the rules are theirs: the manage right on the target, an archived workspace
refused, only one's own conversation.

An agent that already has the agent-builder suite has the first two under the
same names; it then gets ``file_this_conversation`` alone.

**Into a workspace other people see, the person approves the call** (spec 032
FR-022): its members could then search the conversation, and agreeing is the
person's say, never an argument the model sets - content the agent read could
set it. :func:`install_filing_consent` makes every filing call whose target is
not one of the person's own private workspaces pause on the approval card,
whatever the agent's approval settings. The move tool itself files into such a
workspace only for a call the person approved (``app.tools.base.consent_approved``),
decided on the workspace the call resolved to; where no card can be shown (a
channel thread, an MCP client, an assignment run, a sub-agent) it refuses, and
the person files it from the conversation's menu.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition

SUITE_TOOLS = ("list_workspaces", "create_workspace")
FILE_TOOL = "file_this_conversation"
MOVE_TOOL = "move_conversations_to_workspace"
#: The tools that file a conversation into a workspace.
FILING_TOOLS = frozenset({FILE_TOOL, MOVE_TOOL})

_FILE_DEFINITION = ToolDefinition(
    name=FILE_TOOL,
    description=(
        "File the conversation you are in into a workspace the person can change "
        "(by id or exact name). The next messages then work with that workspace's "
        "instructions, sources and skills, and its members can search the "
        "conversation. Into a workspace shared with other people, the person is "
        "asked to approve the call before anything moves."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "The workspace's id, or its exact name.",
            },
        },
        "required": ["workspace_id"],
    },
)


def build_workspace_filing_tools(
    user_id: str,
    *,
    conversation_id: str | None,
    with_listing: bool = True,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The filing suite; *with_listing* False when the agent-builder suite
    already offers ``list_workspaces`` and ``create_workspace``."""
    from .workspaces import build_workspace_tools

    definitions, handlers = build_workspace_tools(user_id, conversation_id=conversation_id)
    wanted = (*SUITE_TOOLS, FILE_TOOL) if with_listing else (FILE_TOOL,)
    executor = CallableToolExecutor()
    for name in wanted:
        executor.register(name, handlers[name])
    offered = [d for d in definitions if with_listing and d.name in SUITE_TOOLS]
    return [*offered, _FILE_DEFINITION], executor


async def private_filing_targets(db: AsyncSession, user_id: uuid.UUID) -> frozenset[str]:
    """What a filing call may name without asking the person: the ids of their
    own workspaces nobody else sees (``workspace_reaches_others``), and the
    folded name or slug of such a workspace when it names exactly one of their
    own - the one ``move_conversations_to_workspace`` resolves a name to. An
    unfiling target asks nobody either."""
    from app.models.workspace import Workspace
    from app.services.workspaces import workspace_reaches_others

    from .workspaces import UNFILE_TARGET

    own = (await db.execute(select(Workspace).where(Workspace.user_id == user_id))).scalars().all()
    targets: set[str] = {UNFILE_TARGET}
    named: dict[str, list[Workspace]] = {}
    for workspace in own:
        for key in {(workspace.name or "").casefold(), (workspace.slug or "").casefold()} - {""}:
            named.setdefault(key, []).append(workspace)
        if not workspace_reaches_others(workspace, user_id):
            targets.add(str(workspace.id))
    for key, rows in named.items():
        if len(rows) == 1 and str(rows[0].id) in targets and key != UNFILE_TARGET:
            targets.add(key)
    return frozenset(targets)


def _filing_target(call: ToolCall) -> str:
    raw = (call.arguments or {}).get("workspace_id") if isinstance(call.arguments, dict) else None
    ref = str(raw or "").strip()
    try:
        return str(uuid.UUID(ref))
    except ValueError:
        return ref.casefold()


def _needs_consent(private: frozenset[str], call: ToolCall) -> bool:
    """A filing call naming anything but a private workspace - an unknown name
    too. A call naming nothing files nothing: the tool says what is missing."""
    target = _filing_target(call)
    return bool(target) and target not in private


class _FilingScreen:
    """The consent screen of the filing tools for one person's turn.

    The person's private workspaces are read when the model first proposes a
    filing call (:meth:`prepare`, awaited by the agent loop before it screens
    the batch), from a session of its own - not on every turn that merely
    offers the tools. Until then, or when the read failed, every call naming a
    workspace asks.
    """

    def __init__(self, user_id: uuid.UUID) -> None:
        self._user_id = user_id
        self._private: frozenset[str] | None = None

    async def prepare(self) -> None:
        if self._private is not None:
            return
        from app.core.database import async_session

        async with async_session() as db:
            self._private = await private_filing_targets(db, self._user_id)

    def __call__(self, call: ToolCall) -> bool:
        from .workspaces import UNFILE_TARGET

        return _needs_consent(self._private if self._private is not None else frozenset({UNFILE_TARGET}), call)


def install_filing_consent(
    composite,
    user_id: str | None,
    *,
    can_ask: bool,
    approved_call_ids: set[str] | None = None,
) -> None:
    """Make a filing call into a workspace other people see pause for the
    person's approval on *composite*, when it offers a filing tool. A call
    naming anything but one of the person's private workspaces asks - an
    unknown or ambiguous name too (:class:`_FilingScreen`).

    Only where the turn can collect an approval (*can_ask*: a streamed chat
    or one that hands its proposals back for a resume). A turn that cannot -
    a channel thread - screens nothing, and the move tool refuses a shared
    target with the way the person can do it themselves."""
    from app.core.utils import safe_uuid

    require = getattr(composite, "require_consent", None)
    uid = safe_uuid(user_id) if user_id else None
    if (
        not can_ask
        or require is None
        or uid is None
        or not FILING_TOOLS & set(getattr(composite, "tool_names", ()))
    ):
        return
    require(FILING_TOOLS, _FilingScreen(uid), approved_call_ids=approved_call_ids)
