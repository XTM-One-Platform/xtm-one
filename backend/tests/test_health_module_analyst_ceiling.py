"""The Module Analyst's ceiling before every model call (#101 S14b-2, dev-docs/platform-health.md 12.5).

The run's provider is wrapped by the engine: each call reserves its worst case on the
analyst's own ledger, books the real usage after, and is refused (no call made, no tool
call answered) over a ceiling or with no price row. The ledger itself is exercised on
PostgreSQL in ``test_health_module_analyst_budget_postgres.py``; here it is replaced.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.llm.base import ChatMessage, ChatResponse
from app.services import assignment_engine as assignment_engine_mod
from app.services.health import budget as health_budget
from app.services.health import cycles as health_cycles
from app.services.health import module_analyst_budget as mab
from app.services.health import module_analyst_ceiling as mac
from app.services.health.settings import HealthModuleAnalystSettings

T0 = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
PRICE = SimpleNamespace(name="price")


class _Inner:
    default_model = "m"
    provider_name = "p"
    last_stop_reason = "end_turn"
    last_usage = {"input_tokens": 10, "output_tokens": 5}

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def chat(self, messages, model=None, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        self.max_tokens = max_tokens
        if self.fail:
            raise RuntimeError("provider down")
        return ChatResponse(content="ok", model="m", usage={"input_tokens": 100, "output_tokens": 20})

    async def chat_stream_with_tools(self, messages, tools=None, **kwargs):
        return await self.chat(messages, tools=tools, **kwargs)


class _Ledger:
    """Stands in for the day/month rows: what reserve and book were asked."""

    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.reserved: list[tuple[int, Decimal, int]] = []
        self.booked: list[dict] = []

    async def reserve(self, db, settings, *, now, tokens, cost, cycle_tokens_used=0):
        if self.refuse:
            return self.refuse
        self.reserved.append((tokens, cost, cycle_tokens_used))
        return None

    async def book(self, db, **kw):
        self.booked.append(kw)


@asynccontextmanager
async def _session():
    async def _noop():
        return None

    yield SimpleNamespace(commit=_noop, rollback=_noop)


def _wrapped(monkeypatch, ledger: _Ledger, *, inner=None, price=PRICE) -> mac.ModuleAnalystCeilingLLM:
    monkeypatch.setattr(mab, "reserve", ledger.reserve)
    monkeypatch.setattr(mab, "book", ledger.book)

    def _priced(usage, p):
        if p is None or not usage:
            return None
        tokens = sum(v for v in usage.values() if isinstance(v, int))
        return SimpleNamespace(total_cost=Decimal(tokens) / Decimal(1000))

    monkeypatch.setattr("app.services.tracing.cost_calculator.price_usage", _priced)

    async def _price(db, model):
        return price

    async def _settings(db):
        return HealthModuleAnalystSettings()

    return mac.ModuleAnalystCeilingLLM(
        inner or _Inner(),
        model="m",
        session_factory=_session,
        price_resolver=_price,
        settings_loader=_settings,
        clock=lambda: T0,
    )


MESSAGES = [ChatMessage(role="user", content="x" * 400)]


async def test_a_call_reserves_its_worst_case_then_books_the_real_usage(monkeypatch):
    ledger = _Ledger()
    llm = _wrapped(monkeypatch, ledger)
    response = await llm.chat(MESSAGES, model="m", max_tokens=1000)
    assert response.content == "ok"
    (tokens, _cost, used_before) = ledger.reserved[0]
    assert tokens == 201 + 1000 and used_before == 0
    assert ledger.booked[0]["reserved_tokens"] == tokens and ledger.booked[0]["spent_tokens"] == 120
    assert ledger.booked[0]["spent_cost"] == Decimal("0.12")
    # The run's cycle counter follows the real usage, not the reservation.
    assert llm.cycle_tokens_used == 120


async def test_the_loop_s_full_output_allowance_is_capped_so_the_first_call_fits_the_cycle(monkeypatch):
    # The loop asks for the model's whole output allowance (64,000 on the lab model):
    # reserved as is, it alone passed the 80,000 per-cycle cap with the prompt and every
    # cycle was refused before its first call (lab measurement, 21 Nov 2026).
    ledger = _Ledger()
    inner = _Inner()
    llm = _wrapped(monkeypatch, ledger, inner=inner)
    await llm.chat_stream_with_tools(MESSAGES, None, max_tokens=64_000)
    (tokens, _cost, _used) = ledger.reserved[0]
    assert tokens == 201 + mac.MAX_CALL_OUTPUT
    assert inner.max_tokens == mac.MAX_CALL_OUTPUT
    assert tokens < HealthModuleAnalystSettings().cycle_tokens


async def test_the_cycle_counter_is_passed_to_the_next_reservation(monkeypatch):
    ledger = _Ledger()
    llm = _wrapped(monkeypatch, ledger)
    await llm.chat(MESSAGES, max_tokens=1000)
    await llm.chat_stream_with_tools(MESSAGES, None, max_tokens=1000)
    assert ledger.reserved[1][2] == 120


@pytest.mark.parametrize(
    "code", [mab.REFUSED_CEILING_REACHED, mab.REFUSED_CYCLE_CEILING_REACHED, mab.REFUSED_CEILING_NOT_SET]
)
async def test_over_a_ceiling_the_call_is_not_made_and_the_run_stops(monkeypatch, code):
    inner = _Inner()
    llm = _wrapped(monkeypatch, _Ledger(refuse=code), inner=inner)
    response = await llm.chat_stream_with_tools(MESSAGES, None, max_tokens=1000)
    assert inner.calls == 0
    assert response.stop_reason == mac.REFUSED_STOP_REASON and not response.tool_calls
    assert response.content == mac.REFUSAL_TEXT[code]
    assert llm.last_stop_reason == mac.REFUSED_STOP_REASON and llm.refusal == code


async def test_with_no_price_the_dollar_ceiling_cannot_hold_so_the_call_is_refused(monkeypatch):
    inner = _Inner()
    ledger = _Ledger()
    llm = _wrapped(monkeypatch, ledger, inner=inner, price=None)
    response = await llm.chat(MESSAGES, max_tokens=1000)
    assert inner.calls == 0 and not ledger.reserved
    assert response.stop_reason == mac.REFUSED_STOP_REASON and llm.refusal == mac.REFUSED_NO_PRICE


async def test_a_failed_call_books_its_reserved_worst_case(monkeypatch):
    ledger = _Ledger()
    llm = _wrapped(monkeypatch, ledger, inner=_Inner(fail=True))
    with pytest.raises(RuntimeError):
        await llm.chat(MESSAGES, max_tokens=1000)
    booked = ledger.booked[0]
    assert booked["spent_tokens"] == booked["reserved_tokens"] and booked["spent_cost"] == booked["reserved_cost"]


async def test_the_wrapper_reads_through_to_the_provider(monkeypatch):
    llm = _wrapped(monkeypatch, _Ledger())
    assert llm.default_model == "m" and llm.provider_name == "p"
    assert llm.last_usage == {"input_tokens": 10, "output_tokens": 5}


def test_the_engine_wraps_the_provider_only_for_the_exempt_module_analyst_run():
    source = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    assert "if _module_analyst_exempt:" in source
    assert "ModuleAnalystCeilingLLM(llm, model=wf_resolved_model)" in source
    assert "llm=_loop_llm," in source


def test_the_module_analyst_leaves_the_fleet_caps():
    # Its cycle is gated by the switches and the licence only, and settling it never
    # charges the fleet ledger or stops it at the fleet caps: one ceiling, one ledger.
    assert "specialist in MODULE_CHECKER_ROLES" in inspect.getsource(health_budget.open_cycle)
    settle = inspect.getsource(health_cycles._settle_cycle)
    assert "own_ledger = specialist in MODULE_CHECKER_ROLES" in settle
    assert "own_ledger=own_ledger" in settle
    assert "None if own_ledger else" in settle


def test_the_estimate_counts_messages_and_tool_definitions():
    tool = SimpleNamespace(name="t", description="d" * 98, parameters={"a": 1})
    assert mac.estimate_input_tokens([ChatMessage(role="user", content="x" * 100)], [tool]) > 100
    assert mac.usage_tokens({"total_tokens": 7}) == 7
    assert mac.usage_tokens({"prompt_tokens": 3, "completion_tokens": 4}) == 7
    assert mac.usage_tokens(None) == 0
