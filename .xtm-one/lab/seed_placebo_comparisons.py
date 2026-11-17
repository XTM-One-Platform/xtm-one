"""The lab's placebo comparisons: three on the sample evaluation, so its Placebo comparison card renders.

Started by ``lab.py`` (``seed_placebo_comparisons``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: a comparison
is only written by a placebo launch (real runs on a real model), so this script writes
``evaluation_placebo_comparisons`` rows through the platform's own model, already settled,
with their statistics (``results``) as ``placebo_stats.comparison_statistics`` shapes them.
Arguments: the evaluation, the user who launched them, the lab agent, two skill ids (the
single-skill targets) and the JSON file the ids are written to.

* the newest, ``completed`` (the card's default): seven rows covering every verdict, the
  targets persona, prompts, skills and two single skills (``skill_loaded`` set on both
  arms, so the Skill opened line renders), one row resting on tokens alone, rows with 1, 2
  and 4 warnings (``few_entries`` included), a long agent name and a long model id;
* a second ``completed`` one with no skill target, so no row shows the Skill opened line;
* the oldest, ``failed``, with no results, so the card shows its status line.

Settled rows are never recomputed: ``settle_finished_comparisons`` reads only the unsettled.
Lab-only sample figures, never production data. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The verdicts of ``placebo_stats`` (``VERDICT_*``), each covered by the first comparison.
VERDICTS: tuple[str, ...] = (
    "beats_placebo", "trade_off", "worse_than_placebo", "no_better_than_placebo", "not_computable",
)
LONG_AGENT_NAME = "Lab sample threat intelligence analyst for the European critical infrastructure desk"
LONG_MODEL_ID = "lab-provider/lab-model-extended-context-reasoning-2026-11-preview-instruct"
METHOD = {
    "resamples": 2000, "confidence": 0.95, "alpha": 0.05, "multiple_comparisons": "holm",
    "quality_selection": None,
}


def _effect(estimate: float, half_width: float, p: float, p_adjusted: float | None) -> dict[str, float | None]:
    return {
        "estimate": estimate, "ci_low": round(estimate - half_width, 3), "ci_high": round(estimate + half_width, 3),
        "p": p, "p_adjusted": p_adjusted,
    }


def _arm(entries: int, trials: int, tokens: float, skill_loaded: float | None = None) -> dict[str, object]:
    return {
        "entries": entries, "trials": trials, "tokens": tokens, "judge": None, "latency": 4.2,
        "tool_calls": 1.5, "skill_loaded": skill_loaded,
    }


def _target(kind: str, skill_id: str | None = None) -> tuple[dict[str, str | None], str]:
    return {"kind": kind, "skill_id": skill_id}, f"skill:{skill_id}" if kind == "skill" else kind


def _row(
    agent: tuple[str, str], model_id: str, target: tuple[str, str | None], verdict: str, *,
    quality: dict | None, tokens: dict | None, warnings: tuple[str, ...] = (), entries: int = 24,
    tokens_alone: bool = False, skill_loaded: tuple[float, float] | None = None, no_target: bool = False,
) -> dict[str, object]:
    kind, skill_id = target
    placebo_target, key = _target(kind, skill_id)
    real_loaded, placebo_loaded = skill_loaded if skill_loaded else (None, None)
    arms: dict[str, object] = {
        "real": _arm(entries, entries * 3, 1840.0, real_loaded),
        "placebo": _arm(entries, entries * 3, 2010.0, placebo_loaded),
    }
    pair = {"quality": _effect(1.2, 2.0, 0.31, None), "tokens": _effect(0.97, 0.05, 0.2, None),
            "entries": entries, "quality_keys": ["quality.overall"]}
    if no_target:
        arms["no_target"] = _arm(entries, entries * 3, 1620.0)
    return {
        "agent_id": agent[0], "agent_name": agent[1], "model_id": model_id, "target": placebo_target,
        "target_key": key, "entries": entries, "arms": arms, "warnings": list(warnings),
        "vs_placebo": {"quality": quality, "tokens": tokens, "entries": entries, "quality_keys": ["quality.overall"]},
        "vs_no_target": pair if no_target else None,
        "placebo_vs_no_target": pair if no_target else None,
        "verdict": verdict, "rests_on_tokens_alone": tokens_alone,
    }


def comparisons(agent_id: str, skill_ids: tuple[str, str]) -> list[dict[str, object]]:
    """The three comparisons, newest first: plain values, no ORM object (pure: unit-tested)."""
    lab = (agent_id, "Lab sample agent")
    long = (str(uuid.uuid5(uuid.NAMESPACE_URL, "lab-placebo-long-agent")), LONG_AGENT_NAME)
    model = "lab-model"
    first = [
        _row(lab, model, ("persona", None), "beats_placebo",
             quality=_effect(8.4, 3.1, 0.001, 0.004), tokens=_effect(0.91, 0.04, 0.002, 0.006),
             warnings=("tokens_partially_measured",), no_target=True),
        _row(lab, model, ("prompts", None), "trade_off",
             quality=_effect(5.2, 2.6, 0.004, 0.012), tokens=_effect(1.18, 0.06, 0.001, 0.005),
             warnings=("corpus_repeated", "length_out_of_tolerance")),
        _row(lab, model, ("skills", None), "beats_placebo",
             quality=_effect(0.6, 1.9, 0.52, 0.52), tokens=_effect(0.82, 0.05, 0.001, 0.005),
             tokens_alone=True, skill_loaded=(0.75, 0.42)),
        _row(lab, LONG_MODEL_ID, ("skill", skill_ids[0]), "worse_than_placebo",
             quality=_effect(-6.3, 2.8, 0.003, 0.012), tokens=_effect(1.04, 0.05, 0.11, 0.33),
             skill_loaded=(0.67, 0.58)),
        _row(long, model, ("skill", skill_ids[1]), "no_better_than_placebo",
             quality=_effect(0.4, 3.5, 0.81, 0.81), tokens=_effect(1.01, 0.07, 0.74, 0.74),
             warnings=("few_entries", "language_undetermined", "token_probe_failed", "sequential_run_mode"),
             entries=6, skill_loaded=(0.5, 0.33)),
        _row(long, LONG_MODEL_ID, ("persona", None), "not_computable",
             quality=None, tokens=_effect(0.99, 0.08, 0.86, None), warnings=("quality_unmeasured",)),
        _row(long, model, ("prompts", None), "not_computable",
             quality=None, tokens=None, warnings=("run_incomplete", "few_entries"), entries=4),
    ]
    second = [
        _row(lab, model, ("persona", None), "no_better_than_placebo",
             quality=_effect(1.1, 2.4, 0.38, 0.76), tokens=_effect(0.98, 0.04, 0.41, 0.82)),
        _row(lab, model, ("prompts", None), "beats_placebo",
             quality=_effect(4.8, 2.2, 0.002, 0.004), tokens=_effect(0.95, 0.04, 0.03, 0.06)),
    ]
    return [
        {"status": "completed", "seed": "lab-placebo-seed-1", "age_hours": 1, "rows": first,
         "targets": [_target("persona")[0], _target("prompts")[0], _target("skills")[0],
                     _target("skill", skill_ids[0])[0], _target("skill", skill_ids[1])[0]],
         "include_no_target": True, "warnings": []},
        {"status": "completed", "seed": "lab-placebo-seed-2", "age_hours": 26, "rows": second,
         "targets": [_target("persona")[0], _target("prompts")[0]], "include_no_target": False, "warnings": []},
        {"status": "failed", "seed": "lab-placebo-seed-3", "age_hours": 50, "rows": None,
         "targets": [_target("persona")[0]], "include_no_target": False,
         "warnings": [{"code": "statistics_failed"}]},
    ]


async def seed(evaluation_id: str, user_id: str, agent_id: str, skill_ids: tuple[str, str]) -> list[str]:
    """Write the three settled comparisons; answer their ids, newest first."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.evaluation import EvaluationPlaceboComparison

    now = datetime.now(UTC)
    rows = []
    async with async_session() as db:
        for spec in comparisons(agent_id, skill_ids):
            started = now - timedelta(hours=int(spec["age_hours"]))
            row = EvaluationPlaceboComparison(
                evaluation_id=uuid.UUID(evaluation_id), status=spec["status"], seed=spec["seed"],
                corpus_version="lab-corpus-v1",
                config={"targets": spec["targets"], "include_no_target": spec["include_no_target"],
                        "trials_per_entry": 3, "custom_text": None},
                created_by=uuid.UUID(user_id),
                results={"method": METHOD, "rows": spec["rows"]} if spec["rows"] is not None else None,
                warnings=spec["warnings"], created_at=started, completed_at=started + timedelta(minutes=40),
            )
            db.add(row)
            rows.append(row)
        await db.commit()
        ids = [str(row.id) for row in rows]
    await engine.dispose()
    return ids


def main() -> None:
    evaluation_id, user_id, agent_id, skill_a, skill_b, out = sys.argv[1:7]
    Path(out).write_text(json.dumps(asyncio.run(seed(evaluation_id, user_id, agent_id, (skill_a, skill_b)))))


if __name__ == "__main__":
    main()
