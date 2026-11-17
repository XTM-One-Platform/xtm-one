# SPDX-License-Identifier: Apache-2.0

"""Guarded replay of Anthropic thinking blocks within a tool round.

Anthropic asks for the thinking blocks of a tool-use turn to be passed back
"complete and unmodified" when its tool results are answered, rejects a
modified block with a 400, and - from Claude Fable 5.1 - accepts a replayed
block only while the system prompt, the tools and the earlier messages are the
ones it was produced under. The agent loop rewrites that prefix routinely
(``load_tools``, the tool-result curation, pruning, compaction, the system
nudges it injects), so a block is re-sent only when a fingerprint of its prefix
proves it unchanged. Every other turn is sent as text + ``tool_use``: the shape
adaptive thinking accepts, and the only one this provider sent before.

* :class:`ThinkingCapture` - one response's blocks, verbatim and in order, on
  the assistant message's in-memory ``_provider_extra`` (never persisted, never
  shown), with the fingerprint of the request they answered.
* :class:`ThinkingReplay` - one request's decision: which captured turns get
  their blocks back, why the others do not, and the trace report.

The blocks are deliberately NOT registered with the agent loop's
provider-extras hook: they are droppable, not required. This provider
owns them - it counts them in its own overflow check and sheds whatever would
not fit (``context_budget``) - and every gate reading that hook acts on the
transcript or the run, so the loop gates stay provider-agnostic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import anthropic

from app.llm.base import ChatMessage
from app.llm.model_capabilities import thinks_by_default
from app.tools.base import ToolCall

logger = logging.getLogger(__name__)

#: The ``_provider_extra`` key a :class:`ThinkingCapture` rides under.
EXTRA_KEY = "anthropic_thinking"
#: The trace metadata key of a request's report.
TRACE_KEY = "thinking_replay"

THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
_ADAPTIVE = "adaptive"

# Why a captured turn went out without its blocks (the trace vocabulary).
REASON_FLAG_OFF = "flag_off"
REASON_NOT_TOOL_ROUND = "not_tool_round"
REASON_MODEL_CHANGED = "model_changed"
REASON_THINKING_CONFIG = "thinking_config"
REASON_TURN_CHANGED = "turn_changed"
REASON_PREFIX_TOOLS = "prefix_changed:tools"
REASON_PREFIX_SYSTEM = "prefix_changed:system"
REASON_PREFIX_MESSAGES = "prefix_changed:messages"
REASON_CONTEXT_BUDGET = "context_budget"
REASON_RETRY_WITHOUT = "retry_without"
#: Strips no later request can undo, so they retire the turn: its tool round
#: has ended, the loop rewrote the turn itself, or the API refused its blocks.
#: A retired turn is never re-sent its blocks; any other strip
#: - a prefix that may come back, another model, thinking off for one call,
#: the context budget - is decided again by the next request.
RETIRING_REASONS = frozenset({REASON_NOT_TOOL_ROUND, REASON_TURN_CHANGED, REASON_RETRY_WITHOUT})

# Bounded like a log line: the report is metadata, not a payload.
_RETRY_ERROR_MAX_CHARS = 300
# How the API names a replayed block it refuses, read off its structured error
# message only: a thinking BLOCK ("`thinking` or `redacted_thinking` blocks in
# the latest assistant message cannot be modified", "Invalid `signature` in
# `thinking` block") or the field path of a content block
# (``messages.1.content.0.thinking.signature``,
# ``...content.0.redacted_thinking.data``). A bare ``thinking`` /
# ``redacted_thinking`` / ``signature`` word is not enough - a tool name or a
# tool schema property (``tools.0.input_schema.properties.thinking...``) can
# carry it, and ``thinking.type.disabled`` is a config refusal, not a
# block one.
_REPLAY_REFUSAL_PATTERNS = (
    re.compile(r"\b(?:redacted_)?thinking[`'\"]?\s+blocks?\b"),
    re.compile(r"\bsignature[`'\"]?\s+(?:in|of|for)\s+(?:the\s+)?[`'\"]?thinking\b"),
    re.compile(r"\bcontent\.\d+\.(?:thinking\.(?:signature|thinking)|redacted_thinking\.data)\b"),
)
# The request path a content validation error names (``messages.3.content.0
# ...``), indexed like the request's ``messages`` array.
_MESSAGE_PATH = re.compile(r"\bmessages\.(\d+)\.content\b")
# How the API refuses a request for its size: a 400 "prompt is too long: N
# tokens > M maximum", or a 413. The pre-flight estimate of a re-sent block is
# chars-based, so the blocks may be what took the request over.
_PROMPT_TOO_LONG = "prompt is too long"

_UNSUPPORTED = object()


def _canonical(value: object) -> bytes:
    """One byte string per value, whatever the dict insertion order.

    ASCII-escaped, so a lone surrogate in a tool result cannot make the
    encoding raise.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
        "ascii"
    )


def digest(value: object) -> str:
    """SHA-256 of the canonical JSON of *value*."""
    return hashlib.sha256(_canonical(value)).hexdigest()


class _MessagesDigest:
    """Running SHA-256 over conversation entries, read up to an index on demand.

    Entries are read lazily, so one at or past the read position may still be
    replaced (a turn getting its blocks back) before it is hashed - what makes
    the prefix of a later turn cover the earlier turns exactly as sent.
    """

    __slots__ = ("_conversation", "_hash", "_position")

    def __init__(self, conversation: list[dict]) -> None:
        self._conversation = conversation
        self._hash = hashlib.sha256()
        self._position = 0

    def before(self, index: int) -> str:
        while self._position < index:
            self._hash.update(_canonical(self._conversation[self._position]))
            self._hash.update(b"\x1e")
            self._position += 1
        return self._hash.copy().hexdigest()

    def full(self) -> str:
        return self.before(len(self._conversation))


@dataclass(frozen=True, slots=True)
class PrefixFingerprint:
    """What a request carried before the turn it produced, as sent."""

    tools: str
    system: str
    messages: str


@dataclass(slots=True)
class ThinkingCapture:
    """A response's content layout, its thinking blocks verbatim.

    ``blocks`` holds the thinking / redacted_thinking blocks exactly as the API
    returned them, the text blocks as returned, and a marker per ``tool_use``
    block (its id): the calls themselves are read from the message's
    ``tool_calls`` at replay time, once ``turn`` proves they are the model's.
    """

    blocks: tuple[dict, ...]
    #: Digest of the turn as the builder renders it without thinking; empty
    #: for a capture that can never be re-sent (``prefix`` ``None``).
    turn: str
    model: str
    family: str | None
    #: ``None`` for a response that cannot be replayed (captured with the
    #: feature off, or answering no tool call).
    prefix: PrefixFingerprint | None
    #: Why the last request carrying this turn sent it without its blocks;
    #: ``None`` once re-sent, and before any request carried it. A reason in
    #: :data:`RETIRING_REASONS` is final; any other is decided again by the
    #: next request.
    stripped_reason: str | None = None

    @property
    def thinking_blocks(self) -> list[dict]:
        return [b for b in self.blocks if b["type"] in THINKING_BLOCK_TYPES]

    def chars(self) -> int:
        return sum(block_chars(b) for b in self.blocks)


def block_chars(block: dict) -> int:
    """Estimated characters of reasoning a replayed block hands back.

    The signature carries the whole reasoning (encrypted, base64) while the
    visible text is at most a summary of it, so the larger of the two is the
    estimate; a redacted block is all ciphertext. Any other block is 0.
    """
    kind = block.get("type")
    if kind == "thinking":
        return max(
            len(block.get("thinking") or ""),
            len(block.get("signature") or "") * 3 // 4,
        )
    if kind == "redacted_thinking":
        return len(block.get("data") or "") * 3 // 4
    return 0


def plain_assistant_content(
    content: str | None, tool_calls: Sequence[ToolCall] | None
) -> list[dict] | str:
    """An assistant turn as text + ``tool_use`` - the shape every turn is sent
    in when its blocks are not re-sent, and the one :attr:`ThinkingCapture.turn`
    is taken over."""
    blocks: list[dict] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in tool_calls or []:
        blocks.append(
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
        )
    return blocks or content or ""


def _layout_entry(kind: str, block: object) -> dict | object:
    if kind == "thinking":
        return {
            "type": "thinking",
            "thinking": getattr(block, "thinking", "") or "",
            "signature": getattr(block, "signature", "") or "",
        }
    if kind == "redacted_thinking":
        return {"type": "redacted_thinking", "data": getattr(block, "data", "") or ""}
    if kind == "text":
        if getattr(block, "citations", None):
            return _UNSUPPORTED
        return {"type": "text", "text": getattr(block, "text", "") or ""}
    if kind == "tool_use":
        return {"type": "tool_use", "id": getattr(block, "id", "") or ""}
    return _UNSUPPORTED


def _finish(layout: list[dict]) -> tuple[dict, ...] | None:
    """The layout to keep, or ``None`` when a thinking block could not be
    re-sent as the API returned it: one without its signature, a redacted
    block without its data."""
    for b in layout:
        if (b["type"] == "thinking" and not b["signature"]) or (
            b["type"] == "redacted_thinking" and not b["data"]
        ):
            return None
    # An empty text block carries nothing and the API refuses one.
    return tuple(b for b in layout if not (b["type"] == "text" and not b["text"]))


def blocks_from_content(content: Iterable[object]) -> tuple[dict, ...] | None:
    """The layout of a non-streamed response (``Message.content``), or ``None``
    when it holds a block that cannot be re-sent as returned."""
    layout: list[dict] = []
    for block in content:
        entry = _layout_entry(getattr(block, "type", ""), block)
        if entry is _UNSUPPORTED:
            return None
        layout.append(entry)  # type: ignore[arg-type]
    return _finish(layout)


class StreamedBlocks:
    """A streamed response's layout, rebuilt from its events the way the SDK
    builds its own snapshot: the thinking text is the concatenated raw
    ``thinking_delta`` text (never the display text of the reasoning pane), the
    ``signature_delta`` carries the whole signature, a redacted block arrives
    whole in its ``content_block_start``.

    The open block's deltas are collected and joined when it closes: a
    ``+=`` on the entry copied the whole block on every delta.
    """

    __slots__ = ("_chunks", "_layout", "_open", "_supported")

    def __init__(self) -> None:
        self._layout: list[dict] = []
        self._open: dict | None = None
        self._chunks: list[str] = []
        self._supported = True

    def start(self, block: object) -> None:
        self._settle()
        entry = _layout_entry(getattr(block, "type", ""), block)
        if entry is _UNSUPPORTED:
            self._supported = False
            self._open = None
            return
        self._layout.append(entry)  # type: ignore[arg-type]
        self._open = entry  # type: ignore[assignment]

    def delta(self, delta: object) -> None:
        entry = self._open
        if entry is None:
            return
        kind = getattr(delta, "type", "")
        if kind == "thinking_delta" and entry["type"] == "thinking":
            self._chunks.append(getattr(delta, "thinking", "") or "")
        elif kind == "signature_delta" and entry["type"] == "thinking":
            entry["signature"] = getattr(delta, "signature", "") or ""
        elif kind == "text_delta" and entry["type"] == "text":
            self._chunks.append(getattr(delta, "text", "") or "")
        elif kind == "citations_delta":
            self._supported = False

    def stop(self) -> None:
        self._settle()
        self._open = None

    def _settle(self) -> None:
        entry = self._open
        if entry is not None and self._chunks:
            field = "thinking" if entry["type"] == "thinking" else "text"
            entry[field] += "".join(self._chunks)
        self._chunks = []

    def layout(self) -> tuple[dict, ...] | None:
        """The response's layout, or ``None`` when it cannot be re-sent - a
        block the stream never closed included: the SDK ends a stream the
        connection closed cleanly without ``message_stop``."""
        if not self._supported or self._open is not None:
            return None
        return _finish(self._layout)


def capture_of(message: object) -> ThinkingCapture | None:
    extra = getattr(message, "_provider_extra", None)
    if not isinstance(extra, dict):
        return None
    capture = extra.get(EXTRA_KEY)
    return capture if isinstance(capture, ThinkingCapture) else None


def thinking_family(thinking: object, model: str) -> str | None:
    """The thinking mode a request runs in: its explicit ``thinking.type``, or
    the model's default (adaptive for the models that think by default)."""
    if isinstance(thinking, dict):
        return str(thinking.get("type") or "") or None
    return _ADAPTIVE if thinks_by_default(model) else None


def _is_tool_results(entry: dict) -> bool:
    content = entry.get("content")
    return (
        entry.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _has_tool_use(entry: dict) -> bool:
    content = entry.get("content")
    return (
        entry.get("role") == "assistant"
        and isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    )


def current_tool_round(conversation: list[dict]) -> frozenset[int]:
    """Indices of the assistant turns whose tool results the request answers:
    the trailing, unbroken run of ``tool_use`` turns, each followed by a user
    turn holding only ``tool_result`` blocks. A human message ends the round;
    a system message sent in place (a loop nudge) does not."""
    indices: list[int] = []
    i = _skip_system(conversation, len(conversation) - 1)
    while i >= 1 and _is_tool_results(conversation[i]) and _has_tool_use(conversation[i - 1]):
        indices.append(i - 1)
        i = _skip_system(conversation, i - 2)
    return frozenset(indices)


def _skip_system(conversation: list[dict], index: int) -> int:
    while index >= 0 and conversation[index].get("role") == "system":
        index -= 1
    return index


def replayed_content(
    capture: ThinkingCapture, tool_calls: Sequence[ToolCall] | None
) -> list[dict] | None:
    """The turn as the model produced it: its blocks in their original order,
    the calls read from the message. ``None`` when the calls no longer line up
    with the captured ``tool_use`` markers."""
    calls = list(tool_calls or [])
    content: list[dict] = []
    position = 0
    for block in capture.blocks:
        kind = block["type"]
        if kind in THINKING_BLOCK_TYPES:
            content.append(dict(block))
        elif kind == "text":
            content.append({"type": "text", "text": block["text"]})
        elif kind == "tool_use":
            if position >= len(calls) or calls[position].id != block["id"]:
                return None
            tc = calls[position]
            position += 1
            content.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
            )
    if position != len(calls):
        return None
    return content


def structured_error_message(exc: anthropic.APIStatusError) -> str:
    """The API's own error message (``body.error.message``), the only text the
    refusal classifiers read - these and the provider's ``thinking.type.disabled``
    check; empty when the body carries none. The SDK's
    rendering folds in the whole response body (a proxy's page, unrelated
    fields), so a word there must never turn a 400 into a replay refusal."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return ""


def _diagnostic_message(exc: BaseException) -> str:
    """What ``retry_error`` and its log line say: the structured message, else
    the HTTP status alone. Never the SDK's rendering of a body without one: a
    proxy's page there can echo the request."""
    message = structured_error_message(exc) if isinstance(exc, anthropic.APIStatusError) else ""
    if message:
        return message
    status = getattr(exc, "status_code", None)
    return f"HTTP {status} (no structured error message)" if status else "no structured error message"


def _is_bad_request(exc: BaseException) -> bool:
    """A 400 at request time. An ``error`` event mid-stream is raised with the
    stream's own 200 response, so it is never one: the API refuses a replayed
    block when it validates the request, before any event."""
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code == 400


def _is_size_refusal(exc: BaseException) -> bool:
    """Return True for the API refusing a request for its size (a 413, or a
    400 whose message says the prompt is too long)."""
    if not isinstance(exc, anthropic.APIStatusError):
        return False
    if exc.status_code == 413:
        return True
    return exc.status_code == 400 and says_prompt_too_long(structured_error_message(exc))


def rejects_replayed_blocks(exc: BaseException) -> bool:
    """Return True for a 400 whose error message refuses a thinking block or
    its signature - what the API answers when it will not take a replayed
    block. Any other 400 (a tool schema, a tool named ``signature``, a refused
    ``thinking.type.disabled``) is not one."""
    return _is_bad_request(exc) and refuses_replayed_blocks(structured_error_message(exc))  # type: ignore[arg-type]


def refuses_replayed_blocks(message: str) -> bool:
    """Whether an API error *message* refuses a thinking block or its signature."""
    message = message.lower()
    return any(pattern.search(message) for pattern in _REPLAY_REFUSAL_PATTERNS)


def says_prompt_too_long(message: str) -> bool:
    """Whether an API error *message* refuses the prompt for its size."""
    return _PROMPT_TOO_LONG in message.lower()


class ThinkingReplay:
    """One request's thinking-replay decision.

    Built by the provider from what the request will send - its model, its
    ``thinking`` config, its tool definitions - before the conversation is
    assembled. :meth:`apply` gives a captured turn its blocks back only when
    (a) the feature is on, (b) the model thinks by default, the request runs in
    the same (adaptive) thinking mode and on the same model as the turn,
    (c) the turn belongs to the request's current tool round, and (d) the turn
    itself and the prefix it was produced under are unchanged. :meth:`capture`
    keeps the response's blocks with the fingerprint of the request as sent.
    """

    def __init__(
        self,
        *,
        model: str,
        thinking: object,
        tools: list[dict] | None,
        enabled: bool,
    ) -> None:
        self.model = model
        self.enabled = enabled
        self.eligible = thinks_by_default(model)
        self.family = thinking_family(thinking, model)
        self._tools = tools
        self._tools_fp: str | None = None
        self._system_fp: str | None = None
        self._stripped: list[dict] | None = None
        self._sent: list[dict] | None = None
        self._digest: _MessagesDigest | None = None
        self._turns: list[tuple[ThinkingCapture, str | None]] = []
        # Request ``messages`` indices of the turns re-sent with their blocks.
        self._replayed_indices: set[int] = set()
        self._retry_error: str | None = None

    @property
    def replayed(self) -> bool:
        """True when the conversation as it stands re-sends at least one block."""
        return any(reason is None for _capture, reason in self._turns)

    def refused_by(self, exc: BaseException) -> bool:
        """Return True when *exc* is the API refusing what this request
        re-sent: a 400 whose error message refuses a thinking block
        (:func:`rejects_replayed_blocks`) or names a turn the replay rewrote
        (``messages.<i>.content...`` - that turn went out as text +
        ``tool_use`` until the replay laid it out again, so whatever the API
        found wrong with it is the replay's to take back), or a refusal of
        the request's size (the blocks' pre-flight estimate is chars-based,
        so they may be what took it over). ``False`` whenever nothing was
        re-sent."""
        if not self.replayed:
            return False
        if _is_size_refusal(exc):
            return True
        if not _is_bad_request(exc):
            return False
        if rejects_replayed_blocks(exc):
            return True
        message = structured_error_message(exc)  # type: ignore[arg-type]
        return any(int(i) in self._replayed_indices for i in _MESSAGE_PATH.findall(message))

    def apply(
        self,
        system: str | None,
        conversation: list[dict],
        sources: Sequence[tuple[int, ChatMessage]],
    ) -> list[dict]:
        """Return *conversation* with the blocks of every provably unchanged
        turn of the current tool round back in place.

        *sources* pairs each assistant entry of *conversation* with the message
        it was built from. *conversation* itself is never modified: a turn that
        gets its blocks back is a new entry in a new list, so the request can
        fall back to exactly what was built.
        """
        self._stripped = conversation
        self._sent = conversation
        if self.enabled and self.eligible:
            # Taken now: prompt caching later adds ``cache_control`` to the
            # last tool definition in place, and the prefix is the request
            # before caching (the markers are not content).
            self._tools_fp = digest(self._tools or None)
            self._system_fp = digest(system or None)
        candidates = [
            (index, message, capture)
            for index, message in sources
            if (capture := capture_of(message)) is not None
        ]
        if not candidates:
            return conversation
        tool_round = current_tool_round(conversation)
        out = list(conversation)
        # A model that does not think by default (a downgrade to Opus 4.8)
        # still reaches the verdict, so the report says why its captured
        # turns went out bare (``model_changed``) - a capture always names a
        # model that does think, so it never gets past that check.
        running = _MessagesDigest(out) if self.enabled else None
        for index, message, capture in candidates:
            if capture.stripped_reason in RETIRING_REASONS:
                reason = capture.stripped_reason
            else:
                reason = self._verdict(index, capture, tool_round, out, running)
            if reason is None:
                content = replayed_content(capture, message.tool_calls)
                if content is None:
                    reason = REASON_TURN_CHANGED
                else:
                    out[index] = {"role": "assistant", "content": content}
                    self._replayed_indices.add(index)
            capture.stripped_reason = reason
            self._turns.append((capture, reason))
        self._digest = running
        if self.replayed:
            self._sent = out
            logger.debug(
                "Anthropic thinking replay (model=%s): %d block(s) re-sent from %d turn(s)",
                self.model,
                sum(len(c.thinking_blocks) for c, r in self._turns if r is None),
                sum(1 for _c, r in self._turns if r is None),
            )
        return self._sent

    def _verdict(
        self,
        index: int,
        capture: ThinkingCapture,
        tool_round: frozenset[int],
        out: list[dict],
        running: _MessagesDigest | None,
    ) -> str | None:
        if index not in tool_round:
            return REASON_NOT_TOOL_ROUND
        if running is None or capture.prefix is None:
            return REASON_FLAG_OFF
        if capture.model != self.model or not self.eligible:
            return REASON_MODEL_CHANGED
        if self.family != _ADAPTIVE or capture.family != self.family:
            return REASON_THINKING_CONFIG
        if digest(out[index].get("content")) != capture.turn:
            return REASON_TURN_CHANGED
        if capture.prefix.tools != self._tools_fp:
            return REASON_PREFIX_TOOLS
        if capture.prefix.system != self._system_fp:
            return REASON_PREFIX_SYSTEM
        if running.before(index) != capture.prefix.messages:
            return REASON_PREFIX_MESSAGES
        return None

    def replayed_chars(self) -> int:
        return sum(capture.chars() for capture, reason in self._turns if reason is None)

    def revert(self, reason: str) -> list[dict]:
        """Send the conversation as built, every re-sent turn stripped for
        *reason*; returns that conversation."""
        turns: list[tuple[ThinkingCapture, str | None]] = []
        for capture, previous in self._turns:
            current = reason if previous is None else previous
            capture.stripped_reason = current
            turns.append((capture, current))
        self._turns = turns
        self._replayed_indices.clear()
        self._sent = self._stripped
        self._digest = None
        return self._stripped or []

    def retry_without(self, exc: BaseException) -> list[dict]:
        """The fail-safe: the API refused the re-sent blocks, so the request
        goes again as built - today's shape - and the trace says why. The
        refused turns are never re-sent again: the same blocks would be
        refused on every later request of the round."""
        message = _diagnostic_message(exc)
        self._retry_error = f"{type(exc).__name__}: {message}"[:_RETRY_ERROR_MAX_CHARS]
        logger.warning(
            "Anthropic refused replayed thinking blocks (model=%s); retrying the "
            "request once without them: %s",
            self.model,
            self._retry_error,
        )
        return self.revert(REASON_RETRY_WITHOUT)

    def capture(
        self,
        layout: tuple[dict, ...] | None,
        content: str | None,
        tool_calls: Sequence[ToolCall] | None,
    ) -> dict | None:
        """The ``_provider_extra`` of a response whose layout is *layout*, or
        ``None`` when it carries no thinking block to keep.

        Only a turn that calls tools can ever be re-sent (a replay lives in a
        tool round), and only with the feature on: only such a turn pays for
        its fingerprints. Any other capture is kept for the report alone
        (``prefix`` ``None``, never re-sent) and nothing is hashed for it.
        """
        if not self.eligible or not layout:
            return None
        if not any(b["type"] in THINKING_BLOCK_TYPES for b in layout):
            return None
        replayable = self.enabled and any(b["type"] == "tool_use" for b in layout)
        return {
            EXTRA_KEY: ThinkingCapture(
                blocks=layout,
                turn=digest(plain_assistant_content(content, tool_calls)) if replayable else "",
                model=self.model,
                family=self.family,
                prefix=self._sent_prefix() if replayable else None,
            )
        }

    def _sent_prefix(self) -> PrefixFingerprint | None:
        if (
            not self.enabled
            or self._sent is None
            or self._tools_fp is None
            or self._system_fp is None
        ):
            return None
        if self._digest is None:
            self._digest = _MessagesDigest(self._sent)
        return PrefixFingerprint(self._tools_fp, self._system_fp, self._digest.full())

    def report(self) -> dict | None:
        """The trace metadata of this request (``None`` when it carried no
        captured turn): what was re-sent, what was not and why."""
        if not self._turns:
            return None
        replayed = [capture for capture, reason in self._turns if reason is None]
        stripped = [(capture, reason) for capture, reason in self._turns if reason is not None]
        if not stripped:
            outcome = "replayed"
        elif replayed:
            outcome = "partial"
        else:
            outcome = "stripped"
        report: dict = {
            "enabled": self.enabled,
            "outcome": outcome,
            "replayed_turns": len(replayed),
            "replayed_blocks": sum(len(c.thinking_blocks) for c in replayed),
            "stripped_turns": len(stripped),
            "stripped_blocks": sum(len(c.thinking_blocks) for c, _r in stripped),
            # The reason of the latest turn sent without its blocks.
            "reason": stripped[-1][1] if stripped else None,
            "reasons": dict(Counter(reason for _c, reason in stripped)),
        }
        if self._retry_error:
            report["retry_error"] = self._retry_error
        return {TRACE_KEY: report}
