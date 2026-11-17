# SPDX-License-Identifier: Apache-2.0

"""Claude thinking blocks on the OpenAI-compatible wire (the AI Gateway's ``thinking_blocks``).

Anthropic refuses a tool-use continuation without them, so the current tool
round's turns get theirs back; a refused replay is retried once without them.
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

    @classmethod
    def build(
        cls, oai_messages: list[dict], messages: Sequence[ChatMessage], provider: str, model: str
    ) -> BlocksReplay:
        """Put back the blocks of every current-round turn captured from this
        provider and model whose calls are still the ones the model made."""
        by_calls: dict[tuple[str, ...], ThinkingBlocks] = {}
        for msg in _current_round(messages):
            kept = (msg._provider_extra or {}).get(EXTRA_KEY)
            if (
                isinstance(kept, ThinkingBlocks)
                and not kept.refused
                and kept.provider == provider
                and kept.model == model
                and kept.call_ids == tuple(tc.id for tc in msg.tool_calls or [])
            ):
                by_calls[kept.call_ids] = kept
        replay = cls(provider, model, oai_messages, oai_messages)
        if not by_calls:
            return replay
        out = list(oai_messages)
        for i, entry in enumerate(out):
            if entry.get("role") != "assistant":
                continue
            kept = by_calls.pop(tuple(tc.get("id") for tc in entry.get("tool_calls") or []), None)
            if kept is not None:
                out[i] = {**entry, "thinking_blocks": [dict(b) for b in kept.blocks]}
                replay.sent.append(kept)
        if replay.sent:
            replay.messages = out
        return replay

    def capture(self, blocks: Sequence[dict], tool_calls: Sequence[ToolCall] | None) -> dict | None:
        """The ``_provider_extra`` of the response, or ``None`` when nothing could be sent back.

        Only a tool turn is ever replayed, and a block without its signature
        (or redacted data) cannot be re-sent as returned.
        """
        if not blocks or not tool_calls or not all(tc.id for tc in tool_calls):
            return None
        for block in blocks:
            if not block.get("signature" if block["type"] == "thinking" else "data"):
                return None
        calls = tuple(tc.id for tc in tool_calls)
        return {EXTRA_KEY: ThinkingBlocks(tuple(blocks), self.provider, self.model, calls)}

    def chars(self) -> int:
        return sum(block_chars(b) for kept in self.sent for b in kept.blocks)

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
        """The request was refused with the blocks: they are never sent again.

        Returns *sent*, the messages as sent (cache breakpoints included), without them.
        """
        logger.warning(
            "Thinking blocks refused (%d turn(s)); retrying once without them: %s",
            len(self.sent),
            reason[:300],
        )
        for kept in self.sent:
            kept.refused = True
        self.retired = self.sent
        self.sent = []
        self.messages = [
            {k: v for k, v in m.items() if k != "thinking_blocks"} if "thinking_blocks" in m else m for m in sent
        ]
        return self.messages
