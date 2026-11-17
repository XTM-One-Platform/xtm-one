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

"""AI-drafted review comments for the Comments panel.

The composer offers "Generate with AI" like the platform's other text fields
(``PromptGenerator``): the reviewer says what the note should convey — "ask
for a stronger title", "the two totals disagree, say so politely" — and gets a
note they can edit before posting. What makes the draft about THIS slide is
assembled here, server-side, from the deck itself: the slide under review (its
layout and text, bounded), where it sits in the outline, the thread being
answered when the draft is a reply, and whatever the reviewer already typed
(an "improve" round keeps their points). The client sends the brief and the
anchor, never the slide — it cannot be trusted to describe the deck, and the
deck is one read away.

Runs on the caller's own model (``get_llm_for_user``) behind
``require_ai_enabled``, on a deck the caller can READ: drafting a note is
reviewing, the same grant that lets them post it. Nothing is persisted — the
draft lands in the textarea and the ordinary ``POST /comments`` posts it (or
not) as the person's own words.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.models.presentation import COMMENT_MAX_CHARS
from app.services.presentation_comments import slide_position, slide_title_of, whole_noun
from app.services.presentation_spec import unit_noun

logger = logging.getLogger(__name__)

#: Longest brief the composer sends — a sentence or three, not an essay.
DRAFT_BRIEF_MAX_CHARS = 2000
#: Bound on the slide JSON shown to the model; a dense slide is a few KB.
_SLIDE_CONTEXT_MAX_CHARS = 6000
#: Bound on each earlier message of a thread quoted to the model.
_THREAD_MESSAGE_MAX_CHARS = 600
#: Messages of a thread quoted to the model: its root plus the most recent
#: replies. A long-lived thread is summarised by the count of what is skipped,
#: so one request never grows with the thread's history.
THREAD_MAX_QUOTED_MESSAGES = 6
#: Tokens for a note: comments are short by construction (4000 chars max).
DRAFT_MAX_TOKENS = 700

# Medium-neutral on purpose: the same composer reviews slide decks and paged
# (A4 / letter) presentations, and the context below (``slide_context``) names
# the unit the note is about with the presentation's own noun — so the prompt
# says "what you are shown" and leaves the noun to it. Kept at or under
# 783 chars: a wording change must not grow prompt text (rule 16).
_SYSTEM_PROMPT = (
    "You draft a review comment on a presentation (a slide deck or a paged "
    "document) for a person who posts it under their own name. Write only "
    "the note: no greeting, no sign-off, no preamble, no quotes around it, no "
    "markdown, no headings, no bullet lists unless the brief asks. Be specific "
    "to what you are shown (name what is on it), constructive and brief — one "
    "to four sentences unless told otherwise. Write in the language of the "
    "reviewer's brief. Never invent facts about what you are shown. The note "
    "speaks in the reviewer's voice TO the presentation's authors: never "
    "address the reviewer, never describe what you could or could not find. If "
    "the brief names something you are not shown, the note asks the authors "
    "about it (where it is, whether it is missing) as a reviewer would."
)


class CommentDraftError(Exception):
    """The model did not return a usable note."""


@dataclass(frozen=True, slots=True)
class CommentDraft:
    """A drafted note, cleaned by :func:`clean_draft`.

    ``truncated`` when the reply was still cut at its output limit after one
    retry with twice the room: the note is returned as drafted, for
    the reviewer to finish before posting it.
    """

    body: str
    truncated: bool = False


def slide_context(spec: dict | None, slide_id: str | None) -> str:
    """What the model is shown about the anchor: the slide, in place; or the deck.

    Speaks the presentation's kind (``unit_noun`` / ``whole_noun``): a page
    brief is about "page 2 of 3" of a presentation, never a slide of a deck.
    """
    spec = spec or {}
    slides = [s for s in (spec.get("slides") or []) if isinstance(s, dict)]
    unit, whole = unit_noun(spec, 1), whole_noun(spec)
    if not slide_id:
        outline = "\n".join(
            f"  {i + 1}. [{s.get('layout')}] {s.get('title') or s.get('subtitle') or '(untitled)'}"
            for i, s in enumerate(slides)
        )
        return (
            f"The note is about the {whole} as a whole ({len(slides)} {unit_noun(spec, len(slides))}). "
            f"Outline:\n{outline}"
        )
    position = slide_position(spec, slide_id)
    slide = next((s for s in slides if s.get("id") == slide_id), None)
    if position is None or slide is None:
        return f"The note is anchored to a {unit} that is no longer in the {whole}."
    shown = {k: v for k, v in slide.items() if k not in ("id", "notes")}
    payload = json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > _SLIDE_CONTEXT_MAX_CHARS:
        payload = payload[: _SLIDE_CONTEXT_MAX_CHARS - 1] + "…"
    title = slide_title_of(spec, slide_id) or "(untitled)"
    neighbours = []
    for offset, label in ((-1, f"Previous {unit}"), (1, f"Next {unit}")):
        index = position - 1 + offset
        if 0 <= index < len(slides):
            other = slides[index]
            neighbours.append(f"{label}: {other.get('title') or other.get('subtitle') or '(untitled)'}")
    around = ("\n" + "\n".join(neighbours)) if neighbours else ""
    return (
        f'The note is about {unit} {position} of {len(slides)} — "{title}" (layout {slide.get("layout")}).'
        f"{around}\n{unit.capitalize()} content (JSON):\n{payload}"
    )


def build_draft_messages(
    *,
    spec: dict | None,
    deck_title: str | None,
    slide_id: str | None,
    brief: str,
    current_text: str | None = None,
    thread: Sequence[tuple[str, str]] = (),
) -> list[ChatMessage]:
    """The conversation the model drafts from.

    ``thread`` is the earlier messages of the thread being replied to, as
    ``(author, text)`` pairs oldest first; empty for a new note. Its root and
    the ``THREAD_MAX_QUOTED_MESSAGES - 1`` most recent replies are quoted,
    each bounded by ``_THREAD_MESSAGE_MAX_CHARS``; anything older is counted.
    """
    parts = [f'Presentation: "{deck_title or "Untitled"}".', slide_context(spec, slide_id)]
    if thread:
        root, replies = thread[0], list(thread[1:])
        skipped = max(0, len(replies) - (THREAD_MAX_QUOTED_MESSAGES - 1))
        quoted_lines = [f"- {root[0] or 'A reviewer'}: {_flatten(root[1], _THREAD_MESSAGE_MAX_CHARS)}"]
        if skipped:
            quoted_lines.append(f"- ({skipped} earlier {'reply' if skipped == 1 else 'replies'} not quoted)")
        quoted_lines.extend(
            f"- {author or 'A reviewer'}: {_flatten(text, _THREAD_MESSAGE_MAX_CHARS)}"
            for author, text in replies[skipped:]
        )
        parts.append("This note is a REPLY in an existing thread:\n" + "\n".join(quoted_lines))
    if current_text and current_text.strip():
        parts.append(
            "The reviewer already wrote this draft; keep its points and improve it "
            "(clearer, more specific, better tone) rather than replacing it:\n"
            f"{_flatten(current_text, COMMENT_MAX_CHARS)}"
        )
    parts.append(f"What the reviewer wants the note to say:\n{brief.strip()}")
    return [
        ChatMessage(role="system", content=_SYSTEM_PROMPT),
        ChatMessage(role="user", content="\n\n".join(parts)),
    ]


_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "«": "»", "‘": "’", "„": "“"}


def _paired_quotes(first: str, last: str) -> bool:
    return _QUOTE_PAIRS.get(first) == last


def clean_draft(raw: str | None) -> str:
    """The model's reply as a note: fences and wrapping quotes gone, bounded."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # A label and a pair of quotes come in either order ("Comment: ..." or
    # Comment: "..."); peel until nothing changes.
    previous = None
    while text != previous:
        previous = text
        for prefix in ("Comment:", "Note:", "Draft:"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix) :].strip()
        if len(text) >= 2 and _paired_quotes(text[0], text[-1]):
            text = text[1:-1].strip()
    if len(text) > COMMENT_MAX_CHARS:
        text = text[: COMMENT_MAX_CHARS - 1].rstrip() + "…"
    return text


async def draft_comment(
    llm,
    model: str | None,
    *,
    spec: dict | None,
    deck_title: str | None,
    slide_id: str | None,
    brief: str,
    current_text: str | None = None,
    thread: Sequence[tuple[str, str]] = (),
) -> CommentDraft:
    """One model call, asked once more when cut at its budget; raises
    :class:`CommentDraftError` on an empty or failed reply."""
    messages = build_draft_messages(
        spec=spec,
        deck_title=deck_title,
        slide_id=slide_id,
        brief=brief,
        current_text=current_text,
        thread=thread,
    )
    async def _draft(max_tokens: int):
        return await llm.chat(messages, model=model, temperature=0.7, max_tokens=max_tokens)

    try:
        response, cut = await chat_retrying_a_cut(llm, model, DRAFT_MAX_TOKENS, _draft, label="Comment draft")
    except Exception as exc:
        logger.warning("Comment draft failed: %s", format_exception_message(exc))
        raise CommentDraftError("The assistant could not draft the note right now. Try again.") from exc
    text = clean_draft(getattr(response, "content", None))
    if not text:
        raise CommentDraftError("The assistant returned an empty note. Try rephrasing what it should say.")
    if cut:
        # Nothing is posted until the reviewer does: the note lands in the
        # textarea, flagged, never withheld.
        logger.warning("Comment draft still cut at max_tokens - returned as drafted")
    return CommentDraft(body=text, truncated=cut)


def _flatten(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = [
    "DRAFT_BRIEF_MAX_CHARS",
    "DRAFT_MAX_TOKENS",
    "CommentDraft",
    "CommentDraftError",
    "build_draft_messages",
    "clean_draft",
    "draft_comment",
    "slide_context",
]
