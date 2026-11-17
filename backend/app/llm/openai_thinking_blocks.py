# SPDX-License-Identifier: Apache-2.0

"""Claude thinking blocks and Gemini thought signatures on the OpenAI-compatible
wire (the AI Gateway's ``thinking_blocks`` and ``tool_calls[].extra_content.google``).

Anthropic refuses a tool-use continuation without its blocks, and Gemini loses
its reasoning between steps without its signatures, so the current tool round's
turns get theirs back; a refused replay is retried once without them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.llm.anthropic_thinking_replay import THINKING_BLOCK_TYPES, block_chars
from app.llm.base import ChatMessage
from app.tools.base import ToolCall

logger = logging.getLogger(__name__)

#: The ``_provider_extra`` key a :class:`ThinkingBlocks` rides under.
EXTRA_KEY = "openai_thinking_blocks"


@dataclass(slots=True)
class ThinkingBlocks:
    """One tool turn's thinking blocks, with what they may be sent back to."""

    blocks: tuple[dict, ...]
    provider: str
    model: str
    call_ids: tuple[str, ...]
    #: Set once a request carrying them was refused: never sent again.
    refused: bool = False
    #: Gemini's ``(call id, thought_signature)`` pairs.
    signatures: tuple[tuple[str, str], ...] = ()


def blocks_of(obj: object) -> list[dict]:
    """The ``thinking_blocks`` a response message or stream delta carries."""
    raw = obj.get("thinking_blocks") if isinstance(obj, Mapping) else getattr(obj, "thinking_blocks", None)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for block in raw:
        if not isinstance(block, Mapping) and hasattr(block, "model_dump"):
            block = block.model_dump()
        if isinstance(block, Mapping) and block.get("type") in THINKING_BLOCK_TYPES:
            out.append(dict(block))
    return out


def thought_signature_of(tool_call: object) -> str | None:
    """Gemini's ``thought_signature`` on a response tool call or its stream delta."""
    extra = (
        tool_call.get("extra_content") if isinstance(tool_call, Mapping) else getattr(tool_call, "extra_content", None)
    )
    google = extra.get("google") if isinstance(extra, Mapping) else None
    signature = google.get("thought_signature") if isinstance(google, Mapping) else None
    return signature if isinstance(signature, str) and signature else None


def _with_signatures(calls: list[dict], signatures: Mapping[str, str]) -> list[dict]:
    return [
        {**call, "extra_content": {"google": {"thought_signature": signatures[call.get("id")]}}}
        if call.get("id") in signatures
        else call
        for call in calls
    ]


def _without_replay(message: dict) -> dict:
    """*message* without the blocks and signatures a replay put on it."""
    calls = message.get("tool_calls") or []
    if "thinking_blocks" not in message and not any("extra_content" in call for call in calls):
        return message
    out = {k: v for k, v in message.items() if k != "thinking_blocks"}
    if calls:
        out["tool_calls"] = [{k: v for k, v in call.items() if k != "extra_content"} for call in calls]
    return out


def _current_round(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """The assistant turns whose tool results the request answers: the trailing
    run of tool-call turns, each followed by its tool messages. System messages
    in between (loop nudges) do not end it."""
    turns: list[ChatMessage] = []
    i = len(messages) - 1
    while True:
        while i >= 0 and messages[i].role == "system":
            i -= 1
        if i < 0 or messages[i].role != "tool":
            return turns
        while i >= 0 and messages[i].role == "tool":
            i -= 1
        if i < 0 or messages[i].role != "assistant" or not messages[i].tool_calls:
            return turns
        turns.append(messages[i])
        i -= 1


@dataclass(slots=True)
class BlocksReplay:
    """One request's replay: what it sends, and the same messages without blocks."""

    provider: str
    model: str
    messages: list[dict]
    without: list[dict]
    sent: list[ThinkingBlocks] = field(default_factory=list)
    retired: list[ThinkingBlocks] = field(default_factory=list)
    #: False when thinking is off: the turns' blocks are not sent, only their signatures.
    with_blocks: bool = True

    @classmethod
    def build(
        cls,
        oai_messages: list[dict],
        messages: Sequence[ChatMessage],
        provider: str,
        model: str,
        *,
        blocks: bool = True,
    ) -> BlocksReplay:
        """Put back the blocks of every current-round turn captured from this
        provider and model whose calls are still the ones the model made; with
        *blocks* off (thinking off), only Gemini's signatures.

        Turns are paired by position from the end, never by call id: the gateway
        restarts Gemini's ids at ``call_0`` every response."""
        wanted: list[ThinkingBlocks | None] = []
        for msg in _current_round(messages):
            kept = (msg._provider_extra or {}).get(EXTRA_KEY)
            usable = (
                isinstance(kept, ThinkingBlocks)
                and not kept.refused
                and kept.provider == provider
                and kept.model == model
                and kept.call_ids == tuple(tc.id for tc in msg.tool_calls or [])
            )
            wanted.append(kept if usable else None)
        replay = cls(provider, model, oai_messages, oai_messages, with_blocks=blocks)
        if not any(wanted):
            return replay
        out = list(oai_messages)
        turns = iter(wanted)
        for i in range(len(out) - 1, -1, -1):
            entry = out[i]
            if entry.get("role") != "assistant" or not entry.get("tool_calls"):
                continue
            kept = next(turns, None)
            if kept is None or kept.call_ids != tuple(tc.get("id") for tc in entry["tool_calls"]):
                continue
            if not kept.signatures and not blocks:
                continue
            entry = {**entry, "tool_calls": _with_signatures(entry["tool_calls"], dict(kept.signatures))}
            if kept.blocks and blocks:
                entry["thinking_blocks"] = [dict(b) for b in kept.blocks]
            out[i] = entry
            replay.sent.append(kept)
        if replay.sent:
            replay.messages = out
        return replay

    def capture(
        self,
        blocks: Sequence[dict],
        tool_calls: Sequence[ToolCall] | None,
        signatures: Mapping[str, str] | None = None,
    ) -> dict | None:
        """The ``_provider_extra`` of the response, or ``None`` when nothing could be sent back.

        Only a tool turn is ever replayed, and a block without its signature
        (or redacted data) cannot be re-sent as returned.
        """
        if not tool_calls or not all(tc.id for tc in tool_calls):
            return None
        calls = tuple(tc.id for tc in tool_calls)
        if any(not block.get("signature" if block["type"] == "thinking" else "data") for block in blocks):
            blocks = ()
        kept_signatures = tuple((call, signatures[call]) for call in calls if call in (signatures or {}))
        if not blocks and not kept_signatures:
            return None
        return {EXTRA_KEY: ThinkingBlocks(tuple(blocks), self.provider, self.model, calls, signatures=kept_signatures)}

    def chars(self) -> int:
        return sum(
            sum(block_chars(b) for b in kept.blocks if self.with_blocks) + sum(len(sig) for _, sig in kept.signatures)
            for kept in self.sent
        )

    def drop(self) -> list[dict]:
        """Send the messages as built: no block this time, decided again next request."""
        self.sent = []
        self.messages = self.without
        return self.without

    def restore(self) -> None:
        """The request failed without the blocks too: they may be sent again."""
        for kept in self.retired:
            kept.refused = False
        self.retired = []

    def retry_without(self, reason: str, sent: list[dict]) -> list[dict]:
        """The request was refused with the blocks (and signatures): they are never sent again.

        Returns *sent*, the messages as sent (cache breakpoints included), without them.
        """
        logger.warning(
            "Thinking blocks or thought signatures refused (%d turn(s)); retrying once without them: %s",
            len(self.sent),
            reason[:300],
        )
        for kept in self.sent:
            kept.refused = True
        self.retired = self.sent
        self.sent = []
        self.messages = [_without_replay(m) for m in sent]
        return self.messages
