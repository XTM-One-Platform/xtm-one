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

"""Recency-positioned framing messages for chat services.

In long conversations, the system prompt at the top can be drowned out
by many history messages.  We mitigate this by injecting one or more
short ``system`` messages BETWEEN the conversation history and the new
user message.  These "framings" steer the LLM to focus on the user's
CURRENT intent rather than getting distracted by prior turns.

Four framings are supported, each with a clear, narrow purpose:

* :func:`channel_thread_framing` — used when the message arrives from a
  public channel thread (Teams, Slack, …) and the agent must stay on
  the thread topic.
* :func:`current_turn_attachment_framing` — used when the user's current
  message has just uploaded new attachment(s) (file/image/screenshot).
  Tells the LLM to treat the freshly-uploaded content as the PRIMARY
  subject and to resolve deictic expressions ("this", "that", "the
  screenshot") to the new attachment, not earlier ones.
* :func:`current_turn_framing` — generic precedence reminder used when
  the conversation history is long enough that earlier instructions
  could overshadow the new user message.
* :func:`interrupted_turn_framing` — used when the user's current
  message is an explicit "continue / resume / go on" instruction after
  a turn was cut short (output truncation, hit max iterations, etc.).
  Asks the LLM to produce a concise checkpoint of completed / pending
  work before resuming, so the user sees a coherent recap instead of
  the LLM silently restarting from scratch or repeating itself.

The :func:`build_turn_framings` helper centralises the gating logic so
``general_chat`` and ``agent_chat`` always agree on which framings fire
in which combination — and so adding new framings only requires changes
in one place.
"""

from __future__ import annotations

import re

from app.llm.base import ChatMessage

# ── Threshold for the generic ``[Current Turn]`` framing ────────────
# Conversations of length ≤ 2 (a single prior round-trip) don't need the
# precedence reminder because there is nothing meaningful to overshadow
# the new message.  Anything longer benefits from it.
_HISTORY_LEN_GENERIC_THRESHOLD = 2

# ── Threshold for the attachment-precedence framing ─────────────────
# Even a single prior message with its own attachment is enough to make
# the LLM mix up "the file" with the prior file — so this threshold is
# stricter (≥ 2) than the generic one above.
_HISTORY_LEN_ATTACHMENT_THRESHOLD = 2

# ── Continuation-request matcher ────────────────────────────────────
# Exact-phrase matching after normalisation.  Intentionally conservative
# (the docstring on ``is_continue_request`` spells out the rationale): a
# scoped instruction like "continue with the deployment checklist" must
# NOT be misclassified as a pure resume signal, because the
# ``interrupted_turn_framing`` would prepend a checkpoint demand that
# would actively damage a fresh-scoped request.  Localisation /
# language-aware matching is a deliberate post-MVP follow-up — see the
# round-3 follow-up that explicitly defers a HITL-button approach so
# users can pick "continue / stop" directly instead of typing.
_CONTINUE_PHRASES = frozenset(
    {
        "continue",
        "please continue",
        "can you continue",
        "yes continue",
        "pls continue",
        "go on",
        "yes go on",
        "go ahead",
        "resume",
        "carry on",
        "proceed",
        "keep going",
        "continue please",
        "keep going please",
    }
)
# Strip common invisible markers often copied from chat apps/editors
# before matching (zero-width space, ZWNJ, ZWJ, BOM).  Without this,
# pasted whitespace from Slack / Notion / Teams would defeat the exact
# phrase match below — the user clearly typed "continue" but their app
# silently inserted a zero-width joiner.
_CONTINUE_TRANSLATION_TABLE = str.maketrans(
    "",
    "",
    "\u200b\u200c\u200d\ufeff",
)


def channel_thread_framing() -> ChatMessage:
    """Framing for messages received in a public channel thread.

    The thread context is already provided in the system prompt above;
    this reminder keeps the LLM focused on the thread topic instead of
    drifting into unrelated personal topics or emails.
    """
    return ChatMessage(
        role="system",
        content=(
            "[Channel Thread] The user is writing in a public "
            "channel thread. The thread context was provided in "
            "the system prompt above — that is the topic of this "
            "conversation. Respond in the context of that thread "
            "discussion. Do NOT bring up unrelated topics, "
            "personal tasks, or emails."
        ),
    )


def current_turn_attachment_framing() -> ChatMessage:
    """Framing fired when the current message has new attachments.

    Without this reminder, the LLM in long conversations confuses
    freshly-uploaded files with attachments from earlier turns —
    especially when the user uses deictic expressions ("the file",
    "this", "that") that could apply to any attachment in scope.

    The text intentionally covers BOTH file uploads and inline images:
    images are inlined directly on the user message (not in the
    ``Available Files`` listing) but the LLM still benefits from the
    explicit "treat the new image as PRIMARY" cue when older images
    are visible earlier in the history.
    """
    return ChatMessage(
        role="system",
        content=(
            "[Current Turn — New Attachment(s)] The user's CURRENT "
            "message has just uploaded new attachment(s) "
            "(file/image/screenshot). Treat the freshly-uploaded "
            "attachment(s) as the PRIMARY subject of this turn — "
            "NOT attachments or topics from earlier turns. When "
            'the user writes deictic expressions like "this", '
            '"that", "the file", "the screenshot", "the '
            'image", or "the attachment" without explicitly '
            "naming an earlier file, they mean the attachment(s) "
            "uploaded WITH this current message. If the user "
            'explicitly corrects you (e.g. "I\'m talking about my '
            'last screenshot, not the previous ones"), obey the '
            "correction immediately."
        ),
    )


def current_turn_framing() -> ChatMessage:
    """Generic recency-positioned reminder for long conversations.

    Reminds the LLM that the new user message takes precedence over
    anything in the conversation history above.  Used when the history
    is long enough that earlier instructions risk shadowing the new
    request, but the new message has no fresh attachments (which would
    instead trigger :func:`current_turn_attachment_framing`).
    """
    return ChatMessage(
        role="system",
        content=(
            "[Current Turn] The following is the user's CURRENT "
            "message. Base your response on this message. If it "
            "corrects, overrides, or narrows earlier instructions "
            "(e.g. specifying a platform, target, or action), "
            "follow the CURRENT message — it takes precedence over "
            "anything in the conversation history above."
        ),
    )


def interrupted_turn_framing() -> ChatMessage:
    """Framing for explicit continuation requests after an interrupted turn.

    Injected when :func:`is_continue_request` matches the current user
    message.  Forces the LLM to start with a structured checkpoint
    (done / pending / errors) before resuming, which prevents three
    failure modes operators saw before this framing existed:

    * The LLM silently restarts from scratch (re-issuing earlier tool
      calls and burning the budget twice).
    * The LLM repeats large chunks of what it already said in the
      previous turn instead of continuing from where it left off.
    * The LLM continues but doesn't surface what was already done, so
      the user can't tell whether the resumed work overlaps with the
      truncated portion.
    """
    return ChatMessage(
        role="system",
        content=(
            "[Continuation Request] The user asked you to continue. "
            "Before doing any new tool calls, start with a concise checkpoint "
            "covering: (1) what was completed so far, (2) what is still pending, "
            "and (3) any errors. Then continue the remaining work and end with a "
            "structured completion summary."
        ),
    )


def is_continue_request(user_message: str) -> bool:
    """Return True when *user_message* is an explicit continuation instruction.

    Conservative by design: we normalise spacing / punctuation /
    invisible markers but still require an exact phrase match against
    :data:`_CONTINUE_PHRASES`.  Substring / prefix matching would
    misclassify scoped requests like ``"continue with the deployment
    checklist"`` as pure resume signals — and the checkpoint demand the
    ``interrupted_turn_framing`` injects on a false positive actively
    damages the response shape for those fresh-scoped requests.

    Localisation is a deliberate post-MVP follow-up: a HITL
    Continue/Stop button in the chat UI removes the keyword-matching
    fragility entirely.  Until that lands, this matcher covers the English
    phrasings users actually type after a truncated turn.
    """
    normalized = (user_message or "").lower()
    normalized = normalized.translate(_CONTINUE_TRANSLATION_TABLE)
    # Two-stage normalization: (1) punctuation → spaces (so "yes,
    # continue" / "continue?" match), (2) collapse all whitespace runs
    # (NBSP, double spaces, tabs) into single spaces so exact-phrase
    # comparison is deterministic across platforms.
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized in _CONTINUE_PHRASES


def message_with_briefs(user_message: str, *briefs: str | None) -> str:
    """The current message as the agent reads it: the user's words, then each brief.

    A brief is a labelled block the platform writes for this turn only (the
    conversations the user referenced; the agents answering after this
    one). It rides on the message sent to the model, never on the
    stored message nor in the system prompt; an empty one adds nothing.
    """
    return "\n\n".join([user_message, *(b for b in briefs if b)])


def build_turn_framings(
    *,
    history_len: int,
    has_current_attachments: bool,
    has_channel_thread_context: bool = False,
    current_user_message: str | None = None,
) -> list[ChatMessage]:
    """Decide which framing messages to inject for the current turn.

    The same gating logic must apply across all chat services
    (``general_chat``, ``agent_chat``, …) — otherwise the platform's
    behaviour silently diverges depending on which entry point handled
    the message.  Centralising it here is the single source of truth.

    Rules:

    * If we are in a channel thread → emit :func:`channel_thread_framing`.
      The generic ``[Current Turn]`` reminder is suppressed because the
      channel-thread reminder already implies "stay on the current
      topic" and adding both can produce contradictory signals.
    * If the current message has new attachments AND the history is
      long enough → emit :func:`current_turn_attachment_framing`.  This
      can stack on top of the channel-thread framing (a user can drop
      a screenshot into a thread).
    * Otherwise, if the history is long enough → emit the generic
      :func:`current_turn_framing` recency reminder.
    * If ``current_user_message`` is provided AND it is an explicit
      continuation request (:func:`is_continue_request`) → append
      :func:`interrupted_turn_framing` at the end so the checkpoint
      demand is the LAST system reminder the LLM sees before the user
      message.  Stacks on top of any of the above — a user can hit
      "continue" inside a channel thread or right after attaching a
      screenshot, and we want both signals.

    The ``current_user_message`` parameter is optional so callers that
    don't have it (background tasks, future entry points) can omit it
    without changing their other framing behaviour — the continuation
    framing is then simply never emitted.  Callers that DO have the
    user message MUST pass it, otherwise the continuation framing is
    silently skipped and we degrade to the behaviour without it.
    """
    framings: list[ChatMessage] = []
    if has_channel_thread_context:
        framings.append(channel_thread_framing())
    if has_current_attachments and history_len >= _HISTORY_LEN_ATTACHMENT_THRESHOLD:
        framings.append(current_turn_attachment_framing())
    if (
        history_len > _HISTORY_LEN_GENERIC_THRESHOLD
        and not has_channel_thread_context
    ):
        framings.append(current_turn_framing())
    if current_user_message is not None and is_continue_request(current_user_message):
        framings.append(interrupted_turn_framing())
    return framings
