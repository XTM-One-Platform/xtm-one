"""The lab's LLM Analytics data: thirty days of traces the /analytics page renders on real rows.

Started by ``lab.py`` (``seed_analytics_traces``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: no API
writes an ``llm_traces`` row (the trace sink does, from a real LLM call), so this
writes them through the platform's models. Arguments: a JSON file naming the agent
ids and the user ids the traces belong to (lab-owned rows created through the API,
the users already in their groups), and the JSON file the counts are written to.

* one trace a day per (agent, user) pair of :func:`plan`, over the last
  :data:`DAYS` days, on :data:`MODELS`, each with one ``GENERATION`` span;
* :data:`ERRORS` failed traces over the last six days, one per message, so Top
  errors lists one row per message on the 7-day and the 30-day windows;
* then the platform's own daily-stats backfill (``run_daily_stats_backfill``),
  twice: the first builds every past day's rollup, the second finds nothing due
  and stamps ``last_aggregation_at``, so the stale-aggregation banner is gone.

So ``/analytics`` shows the KPI cards, the Cost breakdown by agent (20 rows), by
user (15), by group (3) and by model (10), and Top errors (12 rows), with no
fetch override.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

DAYS = 30
#: (model, input price, output price) per million tokens: cost only, nothing is called.
MODELS: tuple[tuple[str, float, float], ...] = (
    ("gpt-4o", 2.5, 10.0),
    ("gpt-4o-mini", 0.15, 0.6),
    ("gpt-4.1", 2.0, 8.0),
    ("o3-mini", 1.1, 4.4),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-haiku-3.5", 0.8, 4.0),
    ("gemini-2.5-pro", 1.25, 10.0),
    ("gemini-2.5-flash", 0.3, 2.5),
    ("mistral-large", 2.0, 6.0),
    ("llama-3.3-70b", 0.6, 0.6),
)
#: The failure messages, one Top errors row each (grouped by message, agent and model).
ERRORS: tuple[str, ...] = (
    "Rate limit exceeded: retry after 20 seconds",
    "Context length exceeded: 131072 tokens maximum",
    "Upstream provider timed out after 120 seconds",
    "Invalid tool call arguments: missing required field 'query'",
    "Content filtered by the provider's safety system",
    "Provider answered 500 Internal Server Error",
    "Provider answered 503 Service Unavailable",
    "Authentication failed: the API key was revoked",
    "Model not found on the provider",
    "Tool result exceeded the 5 MB limit",
    "Stream interrupted before the final chunk",
    "Quota exhausted for the billing period",
)
SOURCES = ("agent-chat", "assignment")


def plan(agents: list[str], users: list[str], now: datetime) -> list[dict]:
    """Every trace to write, times relative to *now*: the successful ones, then one per error message.

    Each agent is used by several users and runs on one model; costs vary by agent, so the breakdown
    tables have distinct rows to sort. Every trace starts on a past day, before *now*.
    """
    rows: list[dict] = []
    for day in range(1, DAYS + 1):
        for a, agent in enumerate(agents):
            if (day + a) % 3:
                continue  # a third of the agents a day: about 200 traces, a varied series
            model, price_in, price_out = MODELS[a % len(MODELS)]
            user = users[(a + day // 3) % len(users)]  # (a + day) alone is a multiple of 3 here
            input_tokens = 800 + 137 * a + 11 * day
            output_tokens = 200 + 41 * a + 5 * day
            rows.append(_row(agent, user, model, now - timedelta(days=day, minutes=7 * a),
                             input_tokens, output_tokens, price_in, price_out, SOURCES[a % 2]))
    for e, message in enumerate(ERRORS):
        agent = agents[e % len(agents)]
        model, price_in, price_out = MODELS[e % len(MODELS)]
        # Within the last six days: the default 7-day window lists every one of them.
        started = now - timedelta(days=1 + e % 6, hours=2, minutes=e)
        row = _row(agent, users[e % len(users)], model, started, 500, 0, price_in, price_out, SOURCES[e % 2])
        row.update(status="error", error_message=message, level="ERROR")
        rows.append(row)
    return rows


def _row(agent: str, user: str, model: str, started: datetime, input_tokens: int, output_tokens: int,
         price_in: float, price_out: float, source: str) -> dict:
    input_cost = round(input_tokens * price_in / 1_000_000, 8)
    output_cost = round(output_tokens * price_out / 1_000_000, 8)
    duration_ms = 900.0 + (input_tokens + output_tokens) % 4000
    return {
        "agent_id": agent, "user_id": user, "attributed_user_id": user, "attribution": "user",
        "source": source, "name": "lab analytics sample", "model": model, "started_at": started,
        "ended_at": started + timedelta(milliseconds=duration_ms), "duration_ms": duration_ms,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
        "input_cost": input_cost, "output_cost": output_cost, "total_cost": round(input_cost + output_cost, 8),
        "status": "ok", "error_message": None, "level": "DEFAULT", "metadata_json": {"seeded_by": "lab"},
    }


async def seed(agents: list[str], users: list[str]) -> dict[str, int]:
    """Write the traces and their spans, then aggregate; answer the counts."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.llm_trace import LLMSpan, LLMTrace
    from app.services.trace_storage import run_daily_stats_backfill

    rows = plan(agents, users, datetime.now(UTC))
    async with async_session() as db:
        for row in rows:
            trace = LLMTrace(
                **{**row, "agent_id": uuid.UUID(row["agent_id"]), "user_id": uuid.UUID(row["user_id"]),
                   "attributed_user_id": uuid.UUID(row["attributed_user_id"])},
                department_resolved_at=row["started_at"],
            )
            db.add(trace)
            await db.flush()
            db.add(LLMSpan(
                trace_id=trace.id, type="GENERATION", name="llm", model=row["model"], level=row["level"],
                status_message=row["error_message"], started_at=row["started_at"], ended_at=row["ended_at"],
                duration_ms=row["duration_ms"], input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
                input_cost=row["input_cost"], output_cost=row["output_cost"], total_cost=row["total_cost"],
            ))
        await db.commit()
    aggregated = 0
    async with async_session() as db:
        # Past days first (the rollups), then a pass with nothing due stamps last_aggregation_at.
        aggregated += await run_daily_stats_backfill(db, retention_days=90, max_days=DAYS + 2)
        aggregated += await run_daily_stats_backfill(db, retention_days=90, max_days=DAYS + 2)
    await engine.dispose()
    return {"traces": len(rows), "errors": len(ERRORS), "daily_stats_rows": aggregated}


def main() -> None:
    ids_file, out = sys.argv[1:3]
    ids = json.loads(Path(ids_file).read_text())
    Path(out).write_text(json.dumps(asyncio.run(seed(ids["agents"], ids["users"]))))


if __name__ == "__main__":
    main()
