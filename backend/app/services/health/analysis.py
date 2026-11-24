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

"""Platform Health analysis layer (#101 S9, ``dev-docs/platform-health.md`` section 6).

The deterministic engine (``engine.py``) never depends on this module. An
analysis is one model call that reads the open findings of one module and
writes a short explanation and an order of fixes. It runs:

* only where AI runs: the scheduler passes its own AI verdict (``ai_on``) and
  the on-demand route is gated by ``require_ai_enabled``;
* only when the administrator switched it on and set every ceiling
  (``HealthAnalysisSettings``): an unset ceiling refuses (``ceiling_not_set``);
* only when the module's open finding set changed (:func:`finding_set_hash`),
  at most once a UTC day per module on the schedule, or on demand; a failed or
  refused attempt counts as that day's attempt, so the schedule retries it the
  next UTC day, never on every pass;
* on the platform routing model, with a static instruction first (prompt
  caching) and no conversation content: findings carry check ids, severities,
  categories, subject ids and measured values only. An agentic finding of a
  module checker (``check_reports.py``, ``evidence.source`` ``module_checker``)
  also carries the tool reads of its evidence and the checker's recommendation,
  bounded, because its measured values live there (#1638).

**Hard ceiling.** Before the call, the worst case (the prompt's estimated input
plus the output cap, priced with the model's price row) is reserved on the day
and the month rows of ``health_analysis_spend`` under a row lock; over any
ceiling the call is refused (``ceiling_reached``) and the page says so. After
the call, the reservation is replaced by the real usage priced by
``price_usage``, the one pricing rule. A call that fails books its reserved
worst case as spent: a provider may bill a timed-out or partly served call,
and an unknown cost is never booked as zero.

**Not the agentic quota.** The call is traced under its own source
(``platform_health``), holds no ``RunContext`` and imports nothing from
``services/quota.py``: no quota counter and no agentic execution count moves
(test (e), ``tests/test_health_analysis.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_analysis import HealthAnalysisSpend, HealthModuleAnalysis
from app.models.health_check import STATUS_OPEN, HealthCheckFinding
from app.services.health.check_reports import AGENTIC_SEGMENT
from app.services.health.check_reports import SOURCE as AGENTIC_SOURCE
from app.services.health.settings import HealthAnalysisSettings

logger = logging.getLogger(__name__)

#: The trace source of every analysis call (``LLM_TRACE_SOURCES``).
TRACE_SOURCE = "platform_health"
#: The output cap of one analysis, and the output side of its reservation.
MAX_OUTPUT_TOKENS = 800
#: The most findings one analysis reads (the most severe first).
MAX_FINDINGS = 60
#: Characters per token used to estimate the prompt before the call (a high estimate):
#: the findings JSON (ids, keys, numbers) measured at 2.2 characters per token in the
#: lab (dev-docs section 6), so 3 under-reserved by a third.
_CHARS_PER_TOKEN = 2
#: The most evidence reads of one agentic finding the prompt carries.
MAX_AGENTIC_READS = 5
#: The longest JSON of one read's values the prompt carries (longer values are cut).
MAX_VALUES_CHARS = 1200
#: The longest recommendation of an agentic finding the prompt carries.
MAX_RECOMMENDATION_CHARS = 400

REFUSED_DISABLED = "disabled"
REFUSED_AI_OFF = "ai_off"
REFUSED_CEILING_NOT_SET = "ceiling_not_set"
REFUSED_CEILING_REACHED = "ceiling_reached"
REFUSED_NO_MODEL = "no_model"
REFUSED_NO_PRICE = "no_price"
UNCHANGED = "unchanged"
ALREADY_TODAY = "already_today"

#: The static part of the prompt, first so a provider can cache it.
SYSTEM_PROMPT = (
    "You are the analysis step of Platform Health, the built-in health suite of an "
    "XTM One platform. You receive the open findings of one module, each a measured "
    "fact from a deterministic check: its check id, severity, category, subject ids "
    "and evidence values. A check id is an identifier, never the name of a component; "
    "a registration_id subject is one registration of the module. A finding checked_by "
    "module_checker carries the tool reads of the module's checker (tool, object ids, "
    "values) and the checker's recommendation. Name a component, service or manager "
    "only when it appears in the values; when they name none, say the evidence does not "
    "name it. Explain in at most 120 words what the findings say together, "
    "then list the fixes in the order an administrator should apply them, one line "
    "each, starting with the critical ones. State only what the evidence shows; when "
    "two findings share a cause, say so. Plain text, no greeting, no markdown table."
)

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class AnalysisOutcome:
    """What one analysis attempt did: ``status`` completed, refused, failed or skipped."""

    module: str
    status: str
    reason: str | None = None
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)


def finding_set_hash(rows: list[HealthCheckFinding]) -> str:
    """sha256 of the open finding ids and their severities, order-free."""
    body = "\n".join(sorted(f"{row.id}:{row.severity}" for row in rows))
    return hashlib.sha256(body.encode()).hexdigest()


def day_period(now: datetime) -> str:
    return "D:" + now.astimezone(UTC).strftime("%Y-%m-%d")


def month_period(now: datetime) -> str:
    return "M:" + now.astimezone(UTC).strftime("%Y-%m")


async def open_findings(db: AsyncSession, module: str) -> list[HealthCheckFinding]:
    rows = (
        await db.execute(
            select(HealthCheckFinding).where(
                HealthCheckFinding.module == module, HealthCheckFinding.status == STATUS_OPEN
            )
        )
    ).scalars()
    return sorted(rows, key=lambda r: (_SEVERITY_ORDER.get(r.severity, 3), r.check_id, r.id))


def _agentic_check_name(check_id: str) -> str:
    """``opencti.agentic.managers_running`` read as ``opencti.managers_running``: the segment is no component."""
    module, sep, check = check_id.partition(f".{AGENTIC_SEGMENT}.")
    return f"{module}.{check}" if sep else check_id


def _bounded_values(values: Any) -> Any:
    if not isinstance(values, dict):
        return {}
    text = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return values if len(text) <= MAX_VALUES_CHARS else {"cut": text[:MAX_VALUES_CHARS]}


def _finding_entry(row: HealthCheckFinding) -> dict[str, Any]:
    evidence = row.evidence or {}
    entry: dict[str, Any] = {
        "check": row.check_id,
        "severity": row.severity,
        "category": row.category,
        "subject": row.subject or {},
    }
    if evidence.get("source") != AGENTIC_SOURCE:
        entry["values"] = evidence.get("values")
        entry["threshold"] = evidence.get("threshold")
        return entry
    reads = [c for c in evidence.get("checks") or [] if isinstance(c, dict)][:MAX_AGENTIC_READS]
    entry["check"] = _agentic_check_name(row.check_id)
    entry["checked_by"] = AGENTIC_SOURCE
    entry["reads"] = [
        {
            "tool": str(c.get("tool") or ""),
            "object_ids": [str(i) for i in c.get("object_ids") or []][:20],
            "values": _bounded_values(c.get("values")),
        }
        for c in reads
    ]
    entry["recommendation"] = (row.recommendation or "")[:MAX_RECOMMENDATION_CHARS] or None
    return entry


def build_prompt(module: str, rows: list[HealthCheckFinding]) -> str:
    """The variable part: the module and its findings as JSON, no free text a person wrote.

    A deterministic finding carries its values and threshold; an agentic finding its
    check name without the ``agentic`` segment, its tool reads and the checker's
    recommendation (:func:`_finding_entry`).
    """
    findings = [_finding_entry(row) for row in rows[:MAX_FINDINGS]]
    payload = {"module": module, "open_findings": len(rows), "findings": findings}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def estimate_input_tokens(*parts: str) -> int:
    return sum(len(part) for part in parts) // _CHARS_PER_TOKEN + 1


async def _locked_periods(db: AsyncSession, now: datetime) -> tuple[HealthAnalysisSpend, HealthAnalysisSpend]:
    """The day and month rows, created when missing and locked in a fixed order (month, then day)."""
    periods = (month_period(now), day_period(now))
    await db.execute(pg_insert(HealthAnalysisSpend).values([{"period": p} for p in periods]).on_conflict_do_nothing())
    rows = {
        row.period: row
        for row in (
            await db.execute(
                select(HealthAnalysisSpend)
                .where(HealthAnalysisSpend.period.in_(periods))
                .order_by(HealthAnalysisSpend.period.desc())
                .with_for_update()
            )
        ).scalars()
    }
    return rows[periods[1]], rows[periods[0]]


def _over(row: HealthAnalysisSpend, tokens: int, cost: Decimal, cap_tokens: int, cap_cost: Decimal) -> bool:
    used_tokens = row.reserved_tokens + row.spent_tokens + tokens
    used_cost = Decimal(row.reserved_cost_usd) + Decimal(row.spent_cost_usd) + cost
    return used_tokens > cap_tokens or used_cost > cap_cost


async def reserve(
    db: AsyncSession, settings: HealthAnalysisSettings, *, now: datetime, tokens: int, cost: Decimal
) -> str | None:
    """Reserve ``tokens`` and ``cost`` on today and this month; the refusal reason, or ``None`` once reserved."""
    if not settings.ceilings_set():
        return REFUSED_CEILING_NOT_SET
    day_tokens, day_cost = settings.day_tokens or 0, settings.day_cost_usd or Decimal(0)
    month_tokens, month_cost = settings.month_tokens or 0, settings.month_cost_usd or Decimal(0)
    day, month = await _locked_periods(db, now)
    if _over(day, tokens, cost, day_tokens, day_cost) or _over(month, tokens, cost, month_tokens, month_cost):
        return REFUSED_CEILING_REACHED
    for row in (day, month):
        row.reserved_tokens += tokens
        row.reserved_cost_usd = Decimal(row.reserved_cost_usd) + cost
    await db.flush()
    return None


async def book(
    db: AsyncSession,
    *,
    now: datetime,
    reserved_tokens: int,
    reserved_cost: Decimal,
    spent_tokens: int,
    spent_cost: Decimal,
) -> None:
    """Release a reservation made at ``now`` and book what was really spent (a failed call books its usage too)."""
    day, month = await _locked_periods(db, now)
    for row in (day, month):
        row.reserved_tokens = max(0, row.reserved_tokens - reserved_tokens)
        row.reserved_cost_usd = max(Decimal(0), Decimal(row.reserved_cost_usd) - reserved_cost)
        row.spent_tokens += spent_tokens
        row.spent_cost_usd = Decimal(row.spent_cost_usd) + spent_cost
    await db.flush()


async def spend(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """Today's and this month's spend, for the page: ``{"day": {...}, "month": {...}}``."""
    rows = {
        row.period: row
        for row in (
            await db.execute(
                select(HealthAnalysisSpend).where(HealthAnalysisSpend.period.in_((day_period(now), month_period(now))))
            )
        ).scalars()
    }
    out: dict[str, dict[str, Any]] = {}
    for name, period in (("day", day_period(now)), ("month", month_period(now))):
        row = rows.get(period)
        out[name] = {
            "tokens": int(row.spent_tokens) if row else 0,
            "cost_usd": str(Decimal(row.spent_cost_usd)) if row else "0",
            "reserved_tokens": int(row.reserved_tokens) if row else 0,
        }
    return out


def _usage_tokens(usage: dict[str, Any] | None) -> int:
    usage = usage or {}
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    keys = ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens")
    return sum(int(usage.get(k) or 0) for k in keys if isinstance(usage.get(k), int | float))


async def _store(
    db: AsyncSession,
    module: str,
    *,
    finding_hash: str,
    status: str,
    reason: str | None,
    trigger: str,
    now: datetime,
    summary: str = "",
    model: str | None = None,
    tokens: int = 0,
    cost: Decimal = Decimal(0),
) -> None:
    row = await db.get(HealthModuleAnalysis, module)
    if row is None:
        row = HealthModuleAnalysis(module=module)
        db.add(row)
    row.finding_hash, row.status, row.reason, row.trigger = finding_hash, status, reason, trigger
    row.summary, row.model, row.tokens, row.cost_usd, row.analysed_at = summary, model, tokens, cost, now
    await db.flush()


async def analyse_module(
    db: AsyncSession,
    module: str,
    settings: HealthAnalysisSettings,
    *,
    now: datetime,
    trigger: str = "schedule",
    llm_factory: Any = None,
) -> AnalysisOutcome:
    """Analyse one module once, within the ceiling. Commits its reservation before the model call.

    ``llm_factory`` (tests) is an async callable ``(db) -> (provider, model)``;
    by default the platform routing model.
    """
    rows = await open_findings(db, module)
    digest = finding_set_hash(rows)
    if not settings.enabled:
        return AnalysisOutcome(module, "refused", REFUSED_DISABLED)
    previous = await db.get(HealthModuleAnalysis, module)
    if trigger == "schedule" and previous is not None:
        if previous.finding_hash == digest and previous.status == "completed":
            return AnalysisOutcome(module, "skipped", UNCHANGED)
        # Any attempt today (completed, failed or refused) is today's attempt: a provider
        # outage or a refusal is retried the next UTC day or on demand, never every pass.
        if previous.analysed_at.astimezone(UTC).date() == now.astimezone(UTC).date():
            return AnalysisOutcome(module, "skipped", ALREADY_TODAY)
    if not rows:
        await _store(db, module, finding_hash=digest, status="completed", reason=None, trigger=trigger, now=now)
        await db.commit()
        return AnalysisOutcome(module, "completed")

    async def _refuse(reason: str) -> AnalysisOutcome:
        await _store(db, module, finding_hash=digest, status="refused", reason=reason, trigger=trigger, now=now)
        await db.commit()
        return AnalysisOutcome(module, "refused", reason)

    if not settings.ceilings_set():
        return await _refuse(REFUSED_CEILING_NOT_SET)

    from app.services.tracing.cost_calculator import price_usage, resolve_model_price_any

    if llm_factory is None:
        llm_factory = _routing_llm
    resolved = await llm_factory(db)
    if resolved is None:
        return await _refuse(REFUSED_NO_MODEL)
    llm, model = resolved
    if not model:
        return await _refuse(REFUSED_NO_MODEL)
    price = await resolve_model_price_any(db, model)
    prompt = build_prompt(module, rows)
    worst_in = estimate_input_tokens(SYSTEM_PROMPT, prompt)
    worst = price_usage({"input_tokens": worst_in, "output_tokens": MAX_OUTPUT_TOKENS}, price)
    if worst is None:
        # Without a price row the dollar ceiling cannot be enforced: refused, never guessed.
        return await _refuse(REFUSED_NO_PRICE)
    reserved_tokens, reserved_cost = worst_in + MAX_OUTPUT_TOKENS, worst.total_cost
    refusal = await reserve(db, settings, now=now, tokens=reserved_tokens, cost=reserved_cost)
    if refusal is not None:
        await db.rollback()
        return await _refuse(refusal)
    await db.commit()

    from app.llm.attribution import provider_config_metadata
    from app.llm.base import ChatMessage
    from app.services.langfuse_tracing import end_trace, trace_llm_call

    trace = await trace_llm_call(
        db,
        name=f"platform-health-analysis:{module}",
        user_id="system",
        source=TRACE_SOURCE,
        model=model,
        input_data={"module": module, "findings": len(rows)},
        metadata={
            "health_module": module,
            "trigger": trigger,
            "llm_provider": getattr(llm, "provider_name", None),
            **provider_config_metadata(llm),
        },
    )
    usage: dict[str, Any] | None = None
    try:
        response = await llm.chat(
            messages=[ChatMessage(role="system", content=SYSTEM_PROMPT), ChatMessage(role="user", content=prompt)],
            model=model,
            temperature=0.2,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        usage = response.usage or {}
        summary = (response.content or "").strip()
    except Exception as exc:
        end_trace(trace, error=str(exc)[:500], usage=usage, model=model)
        logger.warning("Platform Health analysis of %s failed: %s", module, exc)
        # The provider may have billed the call: book the reported usage, else the reserved
        # worst case, never zero, so the ceiling stays hard.
        failed_priced = price_usage(usage, price) if usage else None
        failed_tokens = _usage_tokens(usage or {}) or reserved_tokens
        failed_cost = failed_priced.total_cost if failed_priced is not None else reserved_cost
        await book(
            db,
            now=now,
            reserved_tokens=reserved_tokens,
            reserved_cost=reserved_cost,
            spent_tokens=failed_tokens,
            spent_cost=failed_cost,
        )
        await _store(
            db,
            module,
            finding_hash=digest,
            status="failed",
            reason="model_error",
            trigger=trigger,
            now=now,
            model=model,
            tokens=failed_tokens,
            cost=failed_cost,
        )
        await db.commit()
        return AnalysisOutcome(module, "failed", "model_error")
    end_trace(trace, output=summary, usage=usage, model=model)
    priced = price_usage(usage, price)
    spent_cost = priced.total_cost if priced is not None else reserved_cost
    spent_tokens = _usage_tokens(usage) or reserved_tokens
    await book(
        db,
        now=now,
        reserved_tokens=reserved_tokens,
        reserved_cost=reserved_cost,
        spent_tokens=spent_tokens,
        spent_cost=spent_cost,
    )
    await _store(
        db,
        module,
        finding_hash=digest,
        status="completed",
        reason=None,
        trigger=trigger,
        now=now,
        summary=summary,
        model=model,
        tokens=spent_tokens,
        cost=spent_cost,
    )
    await db.commit()
    return AnalysisOutcome(module, "completed", tokens=spent_tokens, cost_usd=spent_cost)


async def _routing_llm(db: AsyncSession) -> tuple[Any, str] | None:
    """The platform routing (light) model and the config that serves it; ``None`` when none is set."""
    from app.llm.router import get_llm_for_user
    from app.services.flow_llm import routing_model

    model, provider_id = await routing_model(db)
    if not model:
        return None
    llm, resolved = await get_llm_for_user(db, None, model_override=model, provider_id_override=provider_id)
    return llm, resolved or model


async def analysis_tick(*, ai_on: bool, now: datetime | None = None) -> list[AnalysisOutcome]:
    """The scheduler's analysis pass, after the health pass: nothing runs with AI off or analysis off."""
    if not ai_on:
        return []
    from app.api.v1.platform_health_findings import present_modules
    from app.core.database import async_session
    from app.services.health.settings import load_health_settings

    now = now or datetime.now(UTC)
    outcomes: list[AnalysisOutcome] = []
    async with async_session() as db:
        settings = (await load_health_settings(db)).analysis
        if not settings.enabled:
            return []
        for module in present_modules():
            try:
                outcomes.append(await analyse_module(db, module, settings, now=now))
            except Exception:
                await db.rollback()
                logger.exception("Platform Health analysis of %s failed", module)
    return outcomes
