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

"""Presentation Studio — a chat bound to the deck it is editing.

The viewer can dock a conversation next to a deck's live preview. What makes
that conversation a *studio* rather than a chat that happens to be open in the
same window is the binding ``Conversation.presentation_id``: from it the chat
layer derives everything the agent needs to work on the deck without the user
having to say so —

* **context** — which deck, which revision, how many slides and their outline,
  which slide the user is looking at, and what the user may do to it
  (:func:`format_studio_prompt`);
* **capability** — the deck tools are guaranteed on every studio turn, whatever
  the agent's own configuration says (:func:`ensure_studio_tools`). The rail's
  picker only offers agents that already carry ``generate_presentation`` (an
  agent written for detection rules does not become a deck editor by being
  handed the tool for a turn), so for a picked agent the union adds
  ``template_search`` at most; it is what keeps a conversation bound by another
  route — the one that produced the deck, an older studio conversation, an
  explicit ``PUT /chat/conversations/{id}/presentation`` — able to edit even
  when its agent never carried, or has since lost, the tool.

The binding is resolved per turn against the CALLING user's visibility
(:func:`load_studio_context`): a deck that has since been unshared, or deleted,
simply drops out of the context — the conversation goes on, the agent is told
nothing about a deck it may no longer read.

Everything here is deterministic and small. The outline is the same edit map
the deck tools return (``summarize_slides``), so the agent sees one shape for
"the slides" wherever it reads them; the block is emitted BEFORE the security
anchor like every other context section.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.presentation import Presentation
from app.services.presentation_spec import normalize_theme, spec_kind, unit_noun

logger = logging.getLogger(__name__)

#: Built-in tool ids every studio turn carries, on top of the agent's own.
#: ``generate_presentation`` is the deck tool family (``edit_presentation`` —
#: the surgical path — ``get_presentation``, ``search_presentation_text``,
#: ``duplicate_presentation`` and ``delete_presentation`` ride with it);
#: ``template_search`` lets the agent look up the deck's template assets
#: (logos, diagrams) when asked to add one.
STUDIO_TOOL_IDS: tuple[str, ...] = ("generate_presentation", "template_search")

#: How the agent is told what it may do with the deck, per tier.
_ACCESS_NOTES: dict[str, str] = {
    "owner": "you may save edits (the user owns this presentation)",
    "config": "you may save edits (the user can edit this presentation)",
    "read_only": (
        "READ-ONLY - edit_presentation and generate_presentation will refuse to "
        "save it; describe the changes you would make, or offer a copy first "
        "(duplicate_presentation)"
    ),
}


@dataclass(frozen=True, slots=True)
class StudioContext:
    """The deck a conversation is bound to, as the calling user sees it."""

    presentation: Presentation
    #: The user's tier on the deck (``owner`` / ``config`` / ``read_only``).
    permission: str
    #: 1-based index of the slide on the user's screen, when the client sent
    #: one that exists; ``None`` otherwise.
    focused_index: int | None = None
    #: The built-in tools the turn is guaranteed (the video studio's twin,
    #: ``video_studio.studio.VideoStudioContext``, answers the same two).
    tool_ids: tuple[str, ...] = STUDIO_TOOL_IDS

    def prompt(self) -> str:
        return format_studio_prompt(self)


def parse_focused_index(value: object, slide_count: int) -> int | None:
    """Bounds-check the client's "slide on screen" (1-based) against the deck.

    The client sends the index it is showing; a stale or malformed value must
    not turn into an instruction about a slide that does not exist, so anything
    that is not an integer within ``1..slide_count`` is dropped.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > slide_count:
        return None
    return value


async def load_studio_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    presentation_id: uuid.UUID | None,
    focused_index: object = None,
) -> StudioContext | None:
    """Resolve a conversation's deck binding for one turn, or ``None``.

    ``None`` both when the conversation has no binding and when the bound deck
    is not (or no longer) readable by *user_id* — the two are indistinguishable
    on purpose: the agent must not learn that a deck exists from a binding the
    user can no longer follow.
    """
    if presentation_id is None:
        return None
    from app.services.presentations import (
        agent_deck_permission,
        load_visible_presentation,
    )

    loaded = await load_visible_presentation(db, presentation_id, user_id=user_id)
    if loaded is None:
        return None
    presentation, group_ids = loaded
    permission = await agent_deck_permission(db, presentation, user_id=user_id, user_group_ids=group_ids)
    if permission is None:
        return None
    slides = (presentation.spec or {}).get("slides") or []
    return StudioContext(
        presentation=presentation,
        permission=permission,
        focused_index=parse_focused_index(focused_index, len(slides)),
    )


async def studio_turn_active(db: AsyncSession, *, presentation_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """True while an agent turn is in flight in one of *user_id*'s chats bound to the deck.

    The server's own reading of "their assistant is working on it",
    behind the ``assistant`` presence activity. The binding is
    ``Conversation.presentation_id`` (indexed; a user has a handful of studio
    chats per deck at most) and the in-flight signal is the chat turn marker
    the loop itself acquires and releases (``chat_turn_guard``) - the one
    thing that is true exactly as long as the turn is. Scoped to the caller's
    own conversations: each viewer's entry reports that viewer's assistant.

    Fail-open to ``False``: presence is a courtesy, and "no turn" is the safe
    answer when Redis cannot be asked.
    """
    from sqlalchemy import select

    from app.models.chat import Conversation
    from app.services.chat_turn_guard import any_turn_active

    result = await db.execute(
        select(Conversation.id).where(
            Conversation.presentation_id == presentation_id,
            Conversation.user_id == user_id,
        )
    )
    conversation_ids = [str(cid) for cid in result.scalars().all()]
    if not conversation_ids:
        return False
    try:
        return await any_turn_active(conversation_ids)
    except Exception as exc:
        logger.debug("studio turn probe skipped for %s: %s", presentation_id, format_exception_message(exc))
        return False


def ensure_studio_tools(builtin_tool_ids: list[str] | None, tool_ids: tuple[str, ...] = STUDIO_TOOL_IDS) -> list[str]:
    """The turn's built-in tool ids with the studio's guaranteed ones (*tool_ids*) appended.

    Order is preserved and nothing is removed: the agent keeps every tool it
    had, and gains the studio tools it may have lacked. Idempotent.
    """
    resolved = list(builtin_tool_ids or [])
    for tool_id in tool_ids:
        if tool_id not in resolved:
            resolved.append(tool_id)
    return resolved


def format_studio_prompt(ctx: StudioContext) -> str:
    """The system-prompt block for a studio turn.

    Identity, access, the slide in view and the outline — what the user would
    otherwise paste into their first message ("load presentation <id> and keep
    it as the deck we are working on"). The rules are the tool's own contract
    restated in one place: edit THIS deck in place, do not spawn a copy.
    """
    from app.services.builtin_tools.presentation import summarize_slides

    deck = ctx.presentation
    spec = deck.spec or {}
    slides = spec.get("slides") or []
    count = len(slides)
    noun = unit_noun(spec, count)
    kind = spec_kind(spec)
    # The theme is the colour scheme only: a stored legacy ``filigran``
    # is told as the scheme it renders as, never as a third look.
    theme = normalize_theme(spec.get("theme")) or "dark"
    lines = [
        "\n[Presentation Studio]",
        (
            f'The user has "{deck.title}" open next to this chat and is editing it '
            f"with you. presentation_id: {deck.id} - {kind}, {count} {noun}, "
            f"revision {deck.revision}, theme {theme}, "
            f"{spec.get('aspect_ratio') or '16:9'}. Their view refreshes the "
            "moment you save."
        ),
        f"- Access: {_ACCESS_NOTES.get(ctx.permission, _ACCESS_NOTES['read_only'])}.",
        # Routing only: what each deck tool guarantees is the tool's own
        # description (AGENTS.md rule 16 - the system prompt does not restate it).
        (
            "- Every change goes to THIS presentation: call edit_presentation "
            f"with presentation_id={deck.id} (generate_presentation with that id "
            "only to rewrite it wholesale). Never create a new presentation "
            "unless the user asks for a copy."
        ),
        (
            f"- Read the {unit_noun(spec, 1)} you change first (get_presentation, "
            f"selecting only that {unit_noun(spec, 1)}); the outline below is the "
            "current order."
        ),
    ]
    if ctx.focused_index is not None:
        slide = slides[ctx.focused_index - 1]
        title = ""
        if isinstance(slide, dict):
            title = str(slide.get("title") or slide.get("subtitle") or "").strip()
        label = f"{ctx.focused_index}" + (f' ("{title}")' if title else "")
        lines.append(
            f'- The user is looking at {unit_noun(spec, 1)} {label}: "this '
            f'{unit_noun(spec, 1)}", "here" or "it" means that one unless '
            "they say otherwise."
        )
    outline = summarize_slides(spec) if count else "  (empty)"
    lines.append(f"Current {noun}:\n{outline}")
    return "\n".join(lines)
