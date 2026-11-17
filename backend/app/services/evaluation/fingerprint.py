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

"""Deterministic input fingerprinting for prediction reuse.

The fingerprint is a stable SHA-256 over *only* the inputs that affect the
agent's produced output — agent identity, resolved model, execution mode, the
run's config/persona/prompt version snapshots, the entry input + attached file
ids, and (for ``full_loop`` runs) the bound knowledge-base revision + embedding
state. Scoring configuration and scorer selection are **explicitly excluded** so
that scoring-only changes reuse every stored prediction.

A stored prediction is only *reusable* when its source result completed
successfully — non-empty, error-free output from a completed run that is not an
execution anomaly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase
from app.services.evaluation.run_mode import FALLBACK_MODE

# Output prefixes written by the engine's failure paths — never reusable.
_ERROR_OUTPUT_PREFIXES = ("Error:", "Context overflow:")

# The timeline phases of a ``skip_llm`` agent's pipeline tools.
_PIPELINE_PHASES = frozenset({"prepare", "finalize"})


def _pipeline_tool_failed(execution_details: dict) -> bool:
    """Whether a ``skip_llm`` result's timeline holds a failed pipeline tool: its
    output is the tool's error, not an answer. Results stored before such an
    entry was left out as a platform error were scored as answers."""
    if execution_details.get("execution_mode") != "skip_llm":
        return False
    traces = execution_details.get("traces")
    return isinstance(traces, list) and any(
        isinstance(step, dict)
        and step.get("phase") in _PIPELINE_PHASES
        and step.get("outcome") == "error"
        for step in traces
    )


def _canonical(value: Any) -> Any:
    """Return a canonical, order-stable representation for hashing."""
    if isinstance(value, dict):
        return {str(k): _canonical(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def compute_input_fingerprint(
    *,
    agent_id: Any,
    model_id: str | None,
    execution_mode: str | None,
    entry_input: str | None,
    entry_file_ids: list | None = None,
    persona_version: int | None = None,
    prompt_versions: dict | None = None,
    agent_config_versions: dict | None = None,
    kb_state: list | None = None,
    floored_max_iterations: int | None = None,
    apply_prompt_guards: bool = False,
    require_tool_approval: bool = False,
    output_format: str | None = None,
    output_schema: dict | None = None,
    placebo: dict | None = None,
    flow_id: Any = None,
    flow_version: int | None = None,
) -> str:
    """Compute the SHA-256 reuse fingerprint for one entry within a run.

    ``kb_state`` (bound KB revisions + embedding models) is only folded in for
    ``full_loop`` execution — ``llm_only`` runs do not consult knowledge bases,
    so a KB change must not invalidate their reuse.

    ``floored_max_iterations`` is the cap the chat floor raised a low-iteration
    agent to: an answer produced under a lower cap must not be reused as
    one produced under the floor. Left out when ``None`` (the floor did not
    raise the cap), so every other fingerprint stays stable. An owner raising
    the agent to the same cap gets another fingerprint, but its
    ``agent_config_versions`` change with that edit anyway.

    ``apply_prompt_guards`` is folded in for ``full_loop`` only, and only when
    on: an answer the guard may have withheld is not the one a run
    without it would score, and an answer stored without the guard keeps its
    fingerprint under off.

    ``require_tool_approval`` follows the same rule: an answer produced
    behind the approval gate is not the one a run without it would score.

    ``output_format`` and ``output_schema`` count for a JSON producer only: its
    answer goes through ``deliver_json_answer``. Any other agent keeps its fingerprint.

    ``placebo`` is a placebo comparison's control arm: its arm, trial and
    placebo snapshot. Left out for every other run, whose fingerprint is unchanged.

    ``flow_id`` and ``flow_version`` name a flow subject (#8257) in place of the
    agent: every save of a flow writes a new version. Left out for an agent,
    whose fingerprint is unchanged.
    """
    mode = execution_mode or "full_loop"
    subject: dict[str, Any] = (
        {"flow_id": str(flow_id), "flow_version": flow_version}
        if flow_id is not None
        else {"agent_id": str(agent_id)}
    )
    payload: dict[str, Any] = {
        **subject,
        "model_id": model_id or "",
        "execution_mode": mode,
        "entry_input": entry_input or "",
        "entry_file_ids": sorted(str(f) for f in (entry_file_ids or [])),
        "persona_version": persona_version,
        "prompt_versions": _canonical(prompt_versions or {}),
        "agent_config_versions": _canonical(agent_config_versions or {}),
    }
    if mode == "full_loop":
        payload["kb_state"] = _canonical(kb_state or [])
        if floored_max_iterations is not None:
            payload["floored_max_iterations"] = floored_max_iterations
        if apply_prompt_guards:
            payload["apply_prompt_guards"] = True
        if require_tool_approval:
            payload["require_tool_approval"] = True
    if placebo is not None:
        payload["placebo"] = _canonical(placebo)
    if output_format == "json":
        payload["output_contract"] = {
            "format": "json",
            "schema": _canonical(output_schema or {}),
        }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def load_agent_kb_state(db: AsyncSession, agent: Any) -> list[list]:
    """Return a sorted ``[[kb_id, revision, embedding_model], …]`` snapshot.

    Captures every knowledge base bound to the agent so a content change
    (every document writer moves ``KnowledgeBase.revision`` through
    ``kb_helpers.bump_kb_revision``) or an embedding-model change (read here
    from ``embedding_model``, which no revision tracks) invalidates
    ``full_loop`` reuse.
    """
    kb_ids = [str(k) for k in (getattr(agent, "knowledge_base_ids", None) or [])]
    if not kb_ids:
        return []
    result = await db.execute(
        select(
            KnowledgeBase.id,
            KnowledgeBase.revision,
            KnowledgeBase.embedding_model,
        ).where(KnowledgeBase.id.in_(kb_ids))
    )
    rows = [[str(kid), int(rev or 0), emb or ""] for kid, rev, emb in result.all()]
    rows.sort(key=lambda r: r[0])
    return rows


def is_result_reusable(
    agent_output: str | None,
    execution_details: dict | None,
    run_status: str | None = "completed",
    *,
    is_execution_anomaly: bool = False,
) -> bool:
    """True when a stored prediction may be reused.

    Reusable only when the source run completed, the output is non-empty and not
    an engine error marker, the execution was recorded, the result is not an
    execution anomaly, and the agent answered it itself: a ``full_loop`` entry
    answered in LLM Only after its run failed (``llm_only_fallback`` rows from older
    runs) keeps a ``full_loop`` fingerprint and would otherwise be reused
    as the agent's answer. Nor is a ``skip_llm`` result whose pipeline tool
    failed: its output is the tool's error. Error-path results (output prefixed
    ``Error:``/``Context overflow:``) and confirmation-seeking anomalies
    are *non-deliverables*: asking versus delivering is stochastic and not in the
    fingerprint, so a fresh run may still deliver and reuse must not lock the
    anomaly in.
    """
    if is_execution_anomaly:
        return False
    if run_status is not None and run_status != "completed":
        return False
    if not agent_output or not agent_output.strip():
        return False
    if agent_output.startswith(_ERROR_OUTPUT_PREFIXES):
        return False
    if not execution_details:
        return False
    if _pipeline_tool_failed(execution_details):
        return False
    return execution_details.get("execution_mode") != FALLBACK_MODE
