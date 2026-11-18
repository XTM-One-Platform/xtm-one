# SPDX-License-Identifier: Apache-2.0

"""Claude thinking blocks and Gemini thought signatures on the OpenAI-compatible
wire (the AI Gateway's ``thinking_blocks`` and ``tool_calls[].extra_content.google``).

Anthropic refuses a tool-use continuation without its blocks, and Gemini loses
its reasoning between steps without its signatures, so the current tool round's
turns get theirs back; a refused replay is retried once without them.
"""

from __future__ import annotations

import logging
import re
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
    #: Set once a request carrying the blocks was refused: never sent again.
    refused: bool = False
    #: Gemini's ``(call id, thought_signature)`` pairs.
    signatures: tuple[tuple[str, str], ...] = ()
    #: Set once a refusal named the signatures: never sent again.
    signatures_refused: bool = False


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


# How Google names a thought signature in a refusal: ``thought signature(s)``,
# ``thought_signature(s)``, ``thoughtSignature``.
_SIGNATURE_REFUSAL = re.compile(r"\bthought[ _]?signatures?\b")


def refuses_replayed_signatures(message: str) -> bool:
    """Whether an API error *message* names a replayed Gemini thought signature."""
    return _SIGNATURE_REFUSAL.search(message.lower()) is not None


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


def _without_replay(message: dict, *, signatures: bool) -> dict:
    """*message* without the blocks a replay put on it, and its signatures too when *signatures*."""
    calls = message.get("tool_calls") or []
    signed = signatures and any("extra_content" in call for call in calls)
    if "thinking_blocks" not in message and not signed:
        return message
    out = {k: v for k, v in message.items() if k != "thinking_blocks"}
    if signed:
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
    #: The turns whose blocks the last retry retired.
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
            send_blocks = replay._sends_blocks(kept)
            send_signatures = replay._sends_signatures(kept)
            if not send_blocks and not send_signatures:
                continue
            if send_signatures:
                entry = {**entry, "tool_calls": _with_signatures(entry["tool_calls"], dict(kept.signatures))}
            if send_blocks:
                entry = {**entry, "thinking_blocks": [dict(b) for b in kept.blocks]}
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

    def _sends_blocks(self, kept: ThinkingBlocks) -> bool:
        return self.with_blocks and bool(kept.blocks) and not kept.refused

    def _sends_signatures(self, kept: ThinkingBlocks) -> bool:
        return bool(kept.signatures) and not kept.signatures_refused

    def chars(self) -> int:
        return sum(
            sum(block_chars(b) for b in kept.blocks if self._sends_blocks(kept))
            + sum(len(sig) for _, sig in kept.signatures if self._sends_signatures(kept))
            for kept in self.sent
        )

    def drop(self, *, signatures: bool = False) -> bool:
        """Send no block this time, and no signature either with *signatures*:
        decided again next request. Whether anything was dropped."""
        dropped = any(self._sends_blocks(k) or (signatures and self._sends_signatures(k)) for k in self.sent)
        self.with_blocks = False
        self.sent = [] if signatures else [kept for kept in self.sent if self._sends_signatures(kept)]
        self.messages = [_without_replay(m, signatures=False) for m in self.messages] if self.sent else self.without
        return dropped

    def restore(self) -> None:
        """The request failed without the blocks too: they may be sent again.
        A signature a refusal named stays retired."""
        for kept in self.retired:
            kept.refused = False
        self.retired = []

    def retry_without(self, reason: str, sent: list[dict], *, shed_signatures: bool = False) -> list[dict] | None:
        """The request was refused: its blocks are never sent again, nor its
        signatures when *reason* names them. With *shed_signatures* (too big, or
        a refusal that does not say why) they are left out of this retry only. ``None`` when that leaves the request as sent, so there
        is nothing to retry.

        Returns *sent*, the messages as sent (cache breakpoints included), without them.
        """
        named = refuses_replayed_signatures(reason)
        strip = named or shed_signatures
        turns = [(kept, self._sends_blocks(kept), strip and self._sends_signatures(kept)) for kept in self.sent]
        turns = [turn for turn in turns if turn[1] or turn[2]]
        if not turns:
            return None
        logger.warning(
            "%s refused (%d turn(s)); retrying once without them: %s",
            " and ".join(
                what
                for what, sent in (("Thinking blocks", 1), ("thought signatures", 2))
                if any(t[sent] for t in turns)
            ).capitalize(),
            len(turns),
            reason[:300],
        )
        for kept, blocks, signed in turns:
            kept.refused = kept.refused or blocks
            # Only a refusal naming them retires them, and that is final: restore() never brings them back.
            kept.signatures_refused = kept.signatures_refused or (signed and named)
        self.retired = [kept for kept, blocks, _ in turns if blocks]
        self.with_blocks = False
        self.sent = [] if strip else [kept for kept in self.sent if self._sends_signatures(kept)]
        self.messages = [_without_replay(m, signatures=strip) for m in sent]
        return self.messages
