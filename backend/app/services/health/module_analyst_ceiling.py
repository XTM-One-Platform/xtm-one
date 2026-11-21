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

"""The Module Analyst's ceiling, enforced before every model call of its loop (#101 S14b-2, dev-docs 12.5).

The assignment engine wraps the run's provider in :class:`ModuleAnalystCeilingLLM`
when, and only when, :func:`module_analyst_budget.is_module_analyst_run` holds.
Every ``chat`` / ``chat_stream`` / ``chat_stream_with_tools`` call then:

1. reserves its worst case (the estimated input plus the call's output cap) on the
   day and month rows of ``health_module_analyst_spend``, and on the run's own
   per-cycle token cap, in a session of its own (the loop holds none);
2. makes the call through the real provider;
3. books the real usage in place of the reservation, or the reserved worst case
   when the call failed or reported nothing (an unknown cost is never zero).

Over any ceiling, or with no price row for the model (the dollar ceiling cannot be
enforced, so it is refused rather than guessed), the call is not made: the loop gets
an answer with ``stop_reason`` ``budget_exhausted`` and no tool call, so the run ends
with what it has written. The fleet budget of ``budget.py`` never sees this run
(``cycles._book_runs`` skips it): one ceiling, one ledger, no double count.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from app.services.health import module_analyst_budget as mab

logger = logging.getLogger(__name__)

#: The ``stop_reason`` of a refused call (``ChatResponse``: not a completed answer).
REFUSED_STOP_REASON: Final = "budget_exhausted"
#: The output cap reserved when a call names none.
DEFAULT_OUTPUT_CAP: Final = 4096
#: The most output one call of the loop may ask for. The loop asks for the model's whole
#: allowance (64,000 tokens and more on current models), which alone would pass the
#: per-cycle cap and refuse the first call; the call is sent with this cap instead, so
#: what is reserved is what the provider may bill.
MAX_CALL_OUTPUT: Final = 8192
#: Characters per token of the estimate: low on purpose, so the reservation is a worst case.
_CHARS_PER_TOKEN: Final = 2
REFUSED_NO_PRICE: Final = "no_price"

REFUSAL_TEXT: Final[dict[str, str]] = {
    mab.REFUSED_CEILING_NOT_SET: "The Module Analyst's spend limit is not set: the investigation stops here.",
    mab.REFUSED_CEILING_REACHED: "The Module Analyst's daily or monthly spend limit is reached: the investigation stops here.",
    mab.REFUSED_CYCLE_CEILING_REACHED: "The Module Analyst's limit for this cycle is reached: the investigation stops here.",
    REFUSED_NO_PRICE: "The Module Analyst's model has no price: its spend limit cannot be enforced, the investigation stops here.",
}


def _chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def estimate_input_tokens(messages: list[Any], tools: list[Any] | None) -> int:
    """A worst-case input estimate of one call: every message and tool definition, 2 characters a token."""
    chars = 0
    for message in messages or []:
        chars += _chars(getattr(message, "content", None)) + _chars(getattr(message, "tool_calls", None))
    for tool in tools or []:
        chars += (
            _chars(getattr(tool, "name", None))
            + _chars(getattr(tool, "description", None))
            + _chars(getattr(tool, "parameters", None))
        )
    return chars // _CHARS_PER_TOKEN + 1


def usage_tokens(usage: dict[str, Any] | None) -> int:
    """Tokens a usage dict reports (either convention), 0 when it reports none."""
    if not usage:
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        return total
    parts = [usage.get(k) for k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")]
    return sum(p for p in parts if isinstance(p, int) and p > 0)


class ModuleAnalystCeilingLLM:
    """The run's provider, every model call reserved and booked on the Module Analyst ledger.

    Reads every other attribute through to the wrapped provider, so the loop sees the
    same provider (model, context window, last usage).
    """

    def __init__(
        self,
        inner: Any,
        *,
        model: str | None,
        session_factory: Callable[[], Any] | None = None,
        price_resolver: Callable[[Any, str], Awaitable[Any]] | None = None,
        settings_loader: Callable[[Any], Awaitable[Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._model = model
        self._session_factory = session_factory
        self._price_resolver = price_resolver
        self._settings_loader = settings_loader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._price: Any = None
        self._price_resolved = False
        #: What this run (one cycle: the member delegates nothing) spent and holds, in tokens.
        self.cycle_tokens_used = 0
        #: The last refusal, ``None`` while every call fitted.
        self.refusal: str | None = None
        self._stop_override: str | None = None
        self._usage_override: dict | None = None

    # ── pass-through ────────────────────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def last_stop_reason(self) -> Any:
        return (
            self._stop_override if self._stop_override is not None else getattr(self._inner, "last_stop_reason", None)
        )

    @property
    def last_usage(self) -> Any:
        return self._usage_override if self._stop_override is not None else getattr(self._inner, "last_usage", None)

    # ── ledger ──────────────────────────────────────────────────────────
    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from app.core.database import async_session

        return async_session()

    async def _settings(self, db: Any) -> Any:
        if self._settings_loader is not None:
            return await self._settings_loader(db)
        from app.services.health.settings import load_health_settings

        return (await load_health_settings(db)).module_analyst

    async def _price_row(self, db: Any, model: str | None) -> Any:
        if not self._price_resolved:
            self._price_resolved = True
            if model:
                if self._price_resolver is not None:
                    self._price = await self._price_resolver(db, model)
                else:
                    from app.services.tracing.cost_calculator import resolve_model_price_any

                    self._price = await resolve_model_price_any(db, model)
        return self._price

    @staticmethod
    def _priced(usage: dict[str, Any] | None, price: Any) -> Decimal | None:
        from app.services.tracing.cost_calculator import price_usage

        priced = price_usage(usage, price) if usage else None
        return priced.total_cost if priced is not None else None

    async def _reserve(self, messages: list[Any], tools: list[Any] | None, kwargs: dict[str, Any]) -> tuple | None:
        """``(now, tokens, cost)`` once reserved; ``None`` with :attr:`refusal` set when refused."""
        model = kwargs.get("model") or self._model or getattr(self._inner, "default_model", None)
        out_cap = min(int(kwargs.get("max_tokens") or DEFAULT_OUTPUT_CAP), MAX_CALL_OUTPUT)
        # The provider is called with the cap reserved, never more (``kwargs`` is the call's own).
        kwargs["max_tokens"] = out_cap
        tokens = estimate_input_tokens(messages, tools) + int(out_cap)
        now = self._clock()
        async with self._session() as db:
            price = await self._price_row(db, model)
            worst = self._priced({"input_tokens": tokens - int(out_cap), "output_tokens": int(out_cap)}, price)
            if worst is None:
                self.refusal = REFUSED_NO_PRICE
                return None
            refusal = await mab.reserve(
                db,
                await self._settings(db),
                now=now,
                tokens=tokens,
                cost=worst,
                cycle_tokens_used=self.cycle_tokens_used,
            )
            if refusal is not None:
                await db.rollback()
                self.refusal = refusal
                return None
            await db.commit()
        self.cycle_tokens_used += tokens
        return now, tokens, worst

    async def _book(self, held: tuple, usage: dict[str, Any] | None) -> None:
        now, tokens, cost = held
        spent_tokens = usage_tokens(usage) or tokens
        spent_cost = self._priced(usage, self._price)
        if spent_cost is None:
            spent_cost = cost
        try:
            async with self._session() as db:
                await mab.book(
                    db,
                    now=now,
                    reserved_tokens=tokens,
                    reserved_cost=cost,
                    spent_tokens=spent_tokens,
                    spent_cost=spent_cost,
                )
                await db.commit()
        except Exception:
            # The reservation stays held: the ceiling stays hard, never loosened by a failed write.
            logger.exception("Module Analyst: a model call's spend could not be booked")
            return
        self.cycle_tokens_used += spent_tokens - tokens

    def _refused_response(self, model: str | None) -> Any:
        from app.llm.base import ChatResponse

        text = REFUSAL_TEXT.get(self.refusal or "", REFUSAL_TEXT[mab.REFUSED_CEILING_REACHED])
        logger.info("Module Analyst: model call refused (%s)", self.refusal)
        self._stop_override, self._usage_override = REFUSED_STOP_REASON, {}
        return ChatResponse(content=text, model=model or self._model or "", stop_reason=REFUSED_STOP_REASON)

    # ── the three call shapes the loop uses ─────────────────────────────
    async def chat(self, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
        tools = kwargs.get("tools")
        self._stop_override = None
        held = await self._reserve(messages, tools, kwargs)
        if held is None:
            return self._refused_response(kwargs.get("model"))
        usage: dict[str, Any] | None = None
        try:
            response = await self._inner.chat(messages, *args, **kwargs)
            usage = getattr(response, "usage", None) or None
            return response
        finally:
            await self._book(held, usage)

    async def chat_stream_with_tools(self, messages: list[Any], tools: list[Any] | None = None, **kwargs: Any) -> Any:
        self._stop_override = None
        held = await self._reserve(messages, tools, kwargs)
        if held is None:
            return self._refused_response(kwargs.get("model"))
        usage: dict[str, Any] | None = None
        try:
            response = await self._inner.chat_stream_with_tools(messages, tools, **kwargs)
            usage = getattr(response, "usage", None) or None
            return response
        finally:
            await self._book(held, usage)

    async def chat_stream(self, messages: list[Any], *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        self._stop_override = None
        held = await self._reserve(messages, kwargs.get("tools"), kwargs)
        if held is None:
            response = self._refused_response(kwargs.get("model"))
            yield response.content
            return
        try:
            async for chunk in self._inner.chat_stream(messages, *args, **kwargs):
                yield chunk
        finally:
            await self._book(held, getattr(self._inner, "last_usage", None) or None)
