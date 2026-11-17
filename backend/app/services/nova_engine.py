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

"""Nova rule evaluation, backed by XTM One's own model providers.

The Nova engine (`nova-hunting`) evaluates a prompt against `.nov` rules across
four tiers: ``keywords`` / ``condition`` (pure CPU), ``semantics`` (embedding
similarity) and ``llm`` (a model judges the prompt). Its default semantic
backend pulls sentence-transformers + torch (~2 GB), which we do not want in the
image — so instead we **inject our own evaluators**: `NovaMatcher` accepts
``semantic_evaluator=`` / ``llm_evaluator=``, and we point those at the same
embedding provider the knowledge bases use and the platform LLM router. Base
`nova-hunting` is `requests` + `colorama`, so all four tiers run with no ML
dependency of their own.

When `nova-hunting` is not installed at all, evaluation degrades to the built-in
keyword parser in :mod:`app.services.nova_guard` — a rule whose verdict rests on
a semantic/llm tier then simply cannot fire, rather than the guard breaking.

**Sync/async bridge.** Nova's evaluator interface is synchronous, while
embedding and LLM calls are async. Rather than block an event loop inside the
matcher, everything the rules need is resolved *before* matching (one embedding
pass, one LLM pass) and the injected evaluators are thin cache readers. This
also means each distinct pattern costs one call per prompt, not one per rule.

**Off the event loop.** Parsing the rules and running their matchers
is CPU work: it runs in :mod:`app.services.nova_matching`'s worker process,
each rule within a budget, while the pre-computation stays here.

**Cost.** Only the prompt is embedded on every evaluation: a
``semantics:`` pattern's vector is kept in this process per embedding model
(:func:`_embedding_identity`) and pattern text, and dropped when the rules
sync. Every distinct ``llm:`` condition is still asked on every evaluation,
with the same prompt and the same reading of the answer, a few at a time
(``NOVA_EVALUATION_CONCURRENCY``) rather than one after another; only the
opt-in ``NOVA_LLM_MAX_CONDITIONS`` ceiling leaves some unasked, and says so.
Each evaluation logs what it spent.

**Scale.** A ``semantics:`` threshold was written for
``all-MiniLM-L6-v2``; each similarity is mapped onto that model's scale by
:mod:`app.services.nova_calibration` before the worker compares it, and a
model with no measured calibration is scored as before and reported.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib.util
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

import app.core.config as _config
from app.models.nova_rule import NOVA_ACTION_BLOCK, NOVA_ACTION_LOG, NovaRule
from app.services import nova_guard, nova_matching
from app.services.nova_guard import NovaMatch
from app.services.nova_matching import pattern_text as _pattern_text

logger = logging.getLogger(__name__)

NOVA_HUNTING_AVAILABLE = importlib.util.find_spec("nova") is not None

# The llm: tier asks for a yes/no judgement — a couple of tokens is plenty.
_LLM_MAX_TOKENS = 8

# Pattern vectors kept, least recently used dropped first. A synced catalogue
# holds a few hundred distinct patterns; the bound keeps a churning one (or a
# second embedding model) from growing the process past a few tens of MB.
_PATTERN_EMBEDDING_CACHE_MAX = 2048
_pattern_embeddings: OrderedDict[tuple[tuple[str, ...], str], Any] = OrderedDict()
_pattern_embeddings_lock = threading.Lock()

# Rule sets whose ceiling-skipped conditions were already reported.
_REPORTED_SKIPS_MAX = 256
_reported_skips: set[str] = set()


@dataclass
class _EvaluationCost:
    """What one evaluation asked the providers, for its log line."""

    embedding_calls: int = 0
    embedding_cache_hits: int = 0
    llm_conditions: int = 0
    llm_calls: int = 0
    llm_conditions_skipped: int = 0


@dataclass(frozen=True)
class NovaModels:
    """Which models the advanced tiers use. ``None`` = the platform default."""

    llm_model: str | None = None
    semantic_model: str | None = None
    # The embedding provider configuration the semantic override was checked
    # against when it was saved; ``None`` for one saved before it was recorded.
    semantic_provider_id: str | None = None


# ─── Model preferences (owned by the Nova integration) ───────────────────────


async def resolve_nova_models(db) -> NovaModels:
    """Read the per-tier model choice from the Nova integration's config.

    The selectors live in Nova's own admin tab, so the integration row is the
    source of truth; unset means "use the platform default".
    """
    from app.models.integration import Integration
    from app.services.vault import reveal_json_field

    row = (
        await db.execute(
            select(Integration)
            .where(
                Integration.provider == "nova",
                Integration.status == "connected",
                # access_restricted: bypass - platform-wide Nova configuration read, no acting user.
                Integration.is_company_managed.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return NovaModels()
    try:
        extra = await reveal_json_field(row, "extra_config_enc") or {}
    except Exception:
        logger.warning("Could not read Nova integration config", exc_info=True)
        return NovaModels()
    if not isinstance(extra, dict):
        return NovaModels()
    return NovaModels(
        llm_model=(extra.get("nova_llm_model") or None),
        semantic_model=(extra.get("nova_semantic_model") or None),
        semantic_provider_id=(str(extra.get("nova_semantic_provider_id") or "") or None),
    )


#: Why a Nova semantic override is not the model scoring (``SemanticScoring.override_reason``).
OVERRIDE_OF_ANOTHER_PROVIDER = "another_provider"
OVERRIDE_RETURNS_NO_EMBEDDING = "no_embedding"

#: Redis marker: the ``[provider config id, override]`` pair that returned no
#: embedding at evaluation, so the engine and every report agree it is not
#: scoring until it expires, or the override is saved again.
_OVERRIDE_UNUSABLE_KEY = "nova:semantic_override_unusable"
_OVERRIDE_UNUSABLE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class SemanticScoring:
    """What the ``semantics:`` tier scores with. ``model`` is ``None`` when it
    scores nothing; ``dimensions`` is the vector length the provider is asked
    for, ``None`` for the model's native length. ``override_ignored`` is a Nova
    override that is not the model scoring (``override_reason`` says why):
    the tier scores on the platform embedding model instead, so a
    ``semantics:`` block rule is still evaluated."""

    model: str | None = None
    dimensions: int | None = None
    override_ignored: str | None = None
    override_reason: str | None = None


@dataclass(frozen=True)
class _Scorer:
    provider: Any
    provider_id: Any
    default_model: str
    scoring: SemanticScoring


def _unusable_marker(provider_id: Any, override: str) -> str:
    return json.dumps([str(provider_id or ""), override])


async def _override_marked_unusable(provider_id: Any, override: str) -> bool:
    """Whether a recent evaluation found *override* returns no embedding; ``False`` when Redis cannot say."""
    try:
        from app.core.redis import redis_client

        stored = await (await redis_client()).get(_OVERRIDE_UNUSABLE_KEY)
    except Exception:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode("utf-8", "replace")
    return stored == _unusable_marker(provider_id, override)


async def _mark_override_unusable(provider_id: Any, override: str) -> None:
    try:
        from app.core.redis import redis_client

        await (await redis_client()).set(
            _OVERRIDE_UNUSABLE_KEY, _unusable_marker(provider_id, override), ex=_OVERRIDE_UNUSABLE_TTL_SECONDS
        )
    except Exception:
        logger.debug("Could not record the unusable Nova semantic override", exc_info=True)


async def clear_override_unusable_marker() -> None:
    """Forget a recorded unusable override (the override was saved again)."""
    try:
        from app.core.redis import redis_client

        await (await redis_client()).delete(_OVERRIDE_UNUSABLE_KEY)
    except Exception:
        logger.debug("Could not clear the unusable Nova semantic override marker", exc_info=True)


async def _resolve_scorer(db, models: NovaModels) -> _Scorer:
    """THE decision of what the ``semantics:`` tier scores with, read by the
    engine and by every report (engine status, Security Audit, rules tab).

    The Nova override, unless it was saved for another embedding provider
    configuration (or, saved before that was recorded, is a model of another
    provider), or a recent evaluation found it returns no embedding; else the
    platform embedding model. Raises when no embedding provider is configured.
    """
    from app.llm.custom_openai_provider import CustomOpenAIProvider
    from app.llm.openai_provider import OpenAIProvider
    from app.llm.router import find_provider_for_model, get_embedding_provider
    from app.models.llm_config import LLMProviderConfig

    provider, default_model, provider_id = await get_embedding_provider(db)
    override = models.semantic_model
    reason = None
    if override and override != default_model:
        if models.semantic_provider_id:
            if models.semantic_provider_id != str(provider_id or ""):
                reason = OVERRIDE_OF_ANOTHER_PROVIDER
        elif provider_id is not None:
            try:
                owner = await find_provider_for_model(db, override)
                serving = (
                    await db.execute(select(LLMProviderConfig.provider).where(LLMProviderConfig.id == provider_id))
                ).scalar_one_or_none()
            except Exception:
                # An advisory lookup: the evaluation still falls back on a missing vector.
                owner = serving = None
            if owner and serving and owner != serving:
                reason = OVERRIDE_OF_ANOTHER_PROVIDER
        if reason is None and await _override_marked_unusable(provider_id, override):
            reason = OVERRIDE_RETURNS_NO_EMBEDDING
    model = default_model if reason else (override or default_model)
    # The length each provider asks the model for, as it sends it: the OpenAI
    # provider the explicit setting (``None`` = native), the custom one its
    # configured length or the platform default; every other one embeds natively.
    if isinstance(provider, CustomOpenAIProvider):
        from app.models.embedding import EMBEDDING_DIMENSIONS

        dimensions = getattr(provider, "embedding_dimensions", None) or EMBEDDING_DIMENSIONS
    elif isinstance(provider, OpenAIProvider):
        dimensions = getattr(provider, "embedding_dimensions", None)
    else:
        dimensions = None
    return _Scorer(
        provider=provider,
        provider_id=provider_id,
        default_model=default_model,
        scoring=SemanticScoring(
            model=model or None,
            dimensions=dimensions,
            override_ignored=override if reason else None,
            override_reason=reason,
        ),
    )


async def semantic_scoring(db, models: NovaModels | None = None) -> SemanticScoring:
    """The embedding model the ``semantics:`` tier scores with, as the engine
    resolves it (:func:`_resolve_scorer`).

    Scores nothing (``model`` ``None``) when no embedding provider is
    configured, override or not.
    """
    models = models or await resolve_nova_models(db)
    try:
        return (await _resolve_scorer(db, models)).scoring
    except Exception:
        return SemanticScoring()


# ─── Rule loading ────────────────────────────────────────────────────────────


async def load_rule_rows(db, extra_rule_ids: list[str] | None = None) -> list[NovaRule]:
    """Enabled platform rules plus any per-run rules, deduplicated by id."""
    import uuid as _uuid

    rows = list(
        (
            await db.execute(
                select(NovaRule).where(
                    NovaRule.enabled.is_(True),
                    NovaRule.is_company_managed.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    seen = {r.id for r in rows}
    ids: set[_uuid.UUID] = set()
    for raw in extra_rule_ids or []:
        try:
            uid = raw if isinstance(raw, _uuid.UUID) else _uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            continue
        if uid not in seen:
            ids.add(uid)
    if ids:
        rows.extend(
            (
                await db.execute(
                    select(NovaRule).where(
                        NovaRule.id.in_(ids), NovaRule.enabled.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    return rows


# ─── Public entry point ──────────────────────────────────────────────────────


async def evaluate_prompt(
    db,
    prompt: str,
    *,
    user_id: str | None = None,
    extra_rule_ids: list[str] | None = None,
) -> list[NovaMatch]:
    """Evaluate *prompt* against the active rules. Never raises.

    Uses the full Nova engine (all four tiers, our providers) when
    `nova-hunting` is installed; otherwise the built-in keyword tier.
    """
    if not NOVA_HUNTING_AVAILABLE:
        return await nova_guard.evaluate_prompt(
            db, prompt, extra_rule_ids=extra_rule_ids
        )
    try:
        rows = await load_rule_rows(db, extra_rule_ids)
        if not rows:
            return []
        models = await resolve_nova_models(db)
        return await _evaluate_with_nova(db, prompt, rows, models, user_id)
    except Exception:
        logger.warning(
            "Nova engine evaluation failed; falling back to keyword tier",
            exc_info=True,
        )
        return await nova_guard.evaluate_prompt(
            db, prompt, extra_rule_ids=extra_rule_ids
        )


async def _evaluate_with_nova(
    db, prompt: str, rows: list[NovaRule], models: NovaModels, user_id: str | None
) -> list[NovaMatch]:
    """Run the real Nova matcher with our evaluators injected."""
    started = time.perf_counter()
    cost = _EvaluationCost()
    engine_rules = [
        nova_matching.EngineRule(
            label=f"{row.name} ({row.id})",
            text=row.rule_text or "",
            name=row.name,
            category=row.category or "",
            action=row.action or NOVA_ACTION_LOG,
        )
        for row in rows
    ]
    parsed: list[tuple[Any, nova_matching.EngineRule]] = []
    for row, rule, patterns in zip(
        rows, engine_rules, await nova_matching.parse_engine_rules(engine_rules), strict=True
    ):
        if patterns is None:
            logger.info("Skipping unparseable Nova rule %s", row.id)
        else:
            parsed.append((patterns, rule))

    matches: list[NovaMatch] = []
    try:
        # Resolve every advanced-tier input up front (see the module docstring).
        semantic_cache = await _precompute_semantics(
            db,
            prompt,
            [r for r, _ in parsed],
            models.semantic_model,
            semantic_provider_id=models.semantic_provider_id,
            cost=cost,
        )
        llm_cache = await _precompute_llm(
            db,
            prompt,
            [r for r, _ in parsed],
            models.llm_model,
            user_id,
            ask_first=_block_rule_conditions(parsed),
            cost=cost,
        )

        outcomes = await nova_matching.match_engine_rules(
            prompt, [rule for _, rule in parsed], semantic_cache, llm_cache
        )
        for (_, rule), outcome in zip(parsed, outcomes, strict=True):
            if isinstance(outcome, nova_matching.RuleFailed):
                logger.info("Nova rule %s failed to evaluate\n%s", rule.name, outcome.error)
            elif outcome is not None:
                matches.append(outcome)
        return matches
    finally:
        _log_evaluation_cost(cost, rules=len(parsed), matches=len(matches), started=started)


def _block_rule_conditions(
    parsed: list[tuple[Any, nova_matching.EngineRule]],
) -> set[tuple[str, float | None]]:
    """The ``llm:`` conditions of ``block`` rules: asked first under a ceiling."""
    return {
        nova_matching.llm_verdict_key(text, getattr(pattern, "threshold", None))
        for patterns, rule in parsed
        if (rule.action or NOVA_ACTION_LOG) == NOVA_ACTION_BLOCK
        for pattern in (getattr(patterns, "llms", None) or {}).values()
        if (text := _pattern_text(pattern))
    }


def _log_evaluation_cost(cost: _EvaluationCost, *, rules: int, matches: int, started: float) -> None:
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "Nova evaluation: %d rules, %d embedding calls (%d patterns cached), "
        "%d llm calls for %d conditions (%d not asked), %d matches in %.0f ms",
        rules,
        cost.embedding_calls,
        cost.embedding_cache_hits,
        cost.llm_calls,
        cost.llm_conditions,
        cost.llm_conditions_skipped,
        matches,
        duration_ms,
        extra={
            "nova_rules": rules,
            "nova_matches": matches,
            "nova_embedding_calls": cost.embedding_calls,
            "nova_embedding_cache_hits": cost.embedding_cache_hits,
            "nova_llm_conditions": cost.llm_conditions,
            "nova_llm_calls": cost.llm_calls,
            "nova_llm_conditions_skipped": cost.llm_conditions_skipped,
            "nova_duration_ms": duration_ms,
        },
    )


# ─── Provider calls: how many at once ────────────────────────────────────────


def _evaluation_concurrency() -> int:
    """``NOVA_EVALUATION_CONCURRENCY``; one at a time when it cannot be read."""
    try:
        return max(1, int(_config.settings.nova_evaluation_concurrency))
    except (AttributeError, TypeError, ValueError):
        return 1


async def _run_bounded[T](calls: list[Callable[[], Awaitable[T]]], limit: int) -> list[T]:
    """Every call's result, in order, with at most *limit* running at once.

    At a *limit* of 1 the calls are awaited one after another in the calling
    task, as a plain loop would. Otherwise the first exception cancels the
    calls still running and propagates unchanged, as it would out of a loop.
    """
    if limit <= 1 or len(calls) <= 1:
        return [await call() for call in calls]
    slots = asyncio.Semaphore(limit)

    async def _one(call: Callable[[], Awaitable[T]]) -> T:
        async with slots:
            return await call()

    tasks = [asyncio.ensure_future(_one(call)) for call in calls]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# ─── Semantic tier — the KB embedding provider ───────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    import numpy as np

    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)


async def _precompute_semantics(
    db,
    prompt: str,
    rules: list[Any],
    model: str | None,
    *,
    semantic_provider_id: str | None = None,
    cost: _EvaluationCost | None = None,
) -> dict[str, float]:
    """``{pattern text: similarity to the prompt}`` using XTM One's embeddings.

    The prompt is embedded every time; a pattern only when this process holds
    no vector for it from the same embedding model (:func:`_embedding_identity`).
    *model* is the Nova override, scored only where :func:`_resolve_scorer`
    decides it is; one that returns no vector is recorded as such and the
    prompt is scored on the platform embedding model, so a ``semantics:``
    block rule is never left unevaluated by the override.
    """
    cost = cost if cost is not None else _EvaluationCost()
    patterns: set[str] = set()
    for rule in rules:
        for pattern in (getattr(rule, "semantics", None) or {}).values():
            text = _pattern_text(pattern)
            if text:
                patterns.add(text)
    if not patterns:
        return {}

    from app.services.persistent_memory import embed_text_with_model

    # Raises with no embedding provider, as before: the caller falls back to
    # the keyword tier.
    scorer = await _resolve_scorer(db, NovaModels(semantic_model=model, semantic_provider_id=semantic_provider_id))
    provider, provider_id, default_model = scorer.provider, scorer.provider_id, scorer.default_model
    if scorer.scoring.override_ignored:
        _warn_override_once(scorer.scoring.override_ignored, default_model, scorer.scoring.override_reason)
    resolved_model = scorer.scoring.model or default_model
    provider_model = (provider, resolved_model)

    prompt_vec, _ = await embed_text_with_model(db, prompt, _provider_model=provider_model)
    cost.embedding_calls += 1
    if not prompt_vec and resolved_model != default_model:
        await _mark_override_unusable(provider_id, resolved_model)
        _warn_override_once(resolved_model, default_model, OVERRIDE_RETURNS_NO_EMBEDDING)
        resolved_model = default_model
        provider_model = (provider, resolved_model)
        prompt_vec, _ = await embed_text_with_model(db, prompt, _provider_model=provider_model)
        cost.embedding_calls += 1
    if not prompt_vec:
        logger.info("No embedding provider available — semantics tier inert")
        return {}

    identity = _embedding_identity(provider, provider_id, resolved_model)
    vectors: dict[str, Any] = {}
    misses: list[str] = []
    for text in sorted(patterns):
        cached = _cached_pattern_embedding(identity, text, len(prompt_vec))
        if cached is None:
            misses.append(text)
        else:
            vectors[text] = cached
    cost.embedding_cache_hits += len(vectors)
    cost.embedding_calls += len(misses)

    async def _embed(text: str) -> list[float] | None:
        vec, _ = await embed_text_with_model(db, text, _provider_model=provider_model)
        return vec

    embedded = await _run_bounded(
        [functools.partial(_embed, text) for text in misses], _evaluation_concurrency()
    )
    for text, vec in zip(misses, embedded, strict=True):
        if vec:
            vectors[text] = vec
            _remember_pattern_embedding(identity, text, vec)
    from app.services.nova_calibration import calibrate_score

    # On the scale the rule's threshold was written in: unchanged for
    # the reference model and for a model with no measured calibration at this
    # vector length, which the engine status and the Security Audit report.
    return {
        text: calibrate_score(_cosine(prompt_vec, vec), resolved_model, len(prompt_vec))
        for text, vec in vectors.items()
    }


_warned_overrides: set[tuple[str, str | None]] = set()


def _warn_override_once(override: str, fallback: str, reason: str | None) -> None:
    """Say once per process that the Nova semantic override is not scoring."""
    key = (override, reason)
    if key in _warned_overrides:
        return
    _warned_overrides.add(key)
    why = (
        "was saved for another embedding provider"
        if reason == OVERRIDE_OF_ANOTHER_PROVIDER
        else "returned no embedding from the platform embedding provider"
    )
    logger.warning(
        "Nova semantic model override %r %s: the semantics: tier scores on the platform embedding "
        "model %r instead. Pick a model of the platform embedding provider on the Nova rules tab.",
        override,
        why,
        fallback,
    )


def _embedding_identity(provider: Any, provider_id: Any, model: str | None) -> tuple[str, ...]:
    """Everything that decides a pattern's vector, secrets aside.

    The provider configuration and class, the model, the endpoint it is
    served from and the dimensions asked for: changing any of them is a
    different model, whose vectors are not comparable with the old ones.
    """
    client = getattr(provider, "client", None)
    return (
        f"{type(provider).__module__}.{type(provider).__qualname__}",
        str(provider_id or ""),
        str(model or ""),
        str(getattr(provider, "base_url", None) or ""),
        str(getattr(client, "base_url", None) or "") if client is not None else "",
        str(getattr(provider, "aws_region", None) or ""),
        str(getattr(provider, "embedding_dimensions", None) or ""),
    )


def _cached_pattern_embedding(identity: tuple[str, ...], text: str, dimensions: int) -> Any:
    """The kept vector of *text* under *identity*, or ``None``.

    A vector whose length differs from the prompt's is dropped rather than
    compared: it came from a model the identity could not tell apart.
    """
    key = (identity, text)
    with _pattern_embeddings_lock:
        vector = _pattern_embeddings.get(key)
        if vector is None:
            return None
        if len(vector) != dimensions:
            del _pattern_embeddings[key]
            return None
        _pattern_embeddings.move_to_end(key)
        return vector


def _remember_pattern_embedding(identity: tuple[str, ...], text: str, vector: list[float]) -> None:
    import numpy as np

    # The same float64 values ``_cosine`` reads a fresh vector as, read-only.
    kept = np.array(vector, dtype=float)
    kept.setflags(write=False)
    key = (identity, text)
    with _pattern_embeddings_lock:
        _pattern_embeddings[key] = kept
        _pattern_embeddings.move_to_end(key)
        while len(_pattern_embeddings) > _PATTERN_EMBEDDING_CACHE_MAX:
            _pattern_embeddings.popitem(last=False)


def clear_pattern_embedding_cache() -> None:
    """Drop every kept pattern vector (the rules synced).

    A changed pattern is a new key anyway: this frees the vectors of patterns
    the rules no longer carry.
    """
    with _pattern_embeddings_lock:
        _pattern_embeddings.clear()


# ─── LLM tier — the platform LLM router ──────────────────────────────────────

_LLM_SYSTEM_PROMPT = (
    "You are a strict prompt-safety classifier. Answer with a single word: "
    "YES if the user's message matches the described condition, NO otherwise."
)


async def _precompute_llm(
    db,
    prompt: str,
    rules: list[Any],
    model: str | None,
    user_id: str | None,
    *,
    ask_first: Iterable[tuple[str, float | None]] = (),
    cost: _EvaluationCost | None = None,
) -> dict[tuple[str, float | None], bool]:
    """``{(llm pattern prompt, temperature): verdict}`` judged by the platform LLM.

    Nova uses a pattern's threshold as the temperature of its LLM call,
    so each condition is judged at the temperature its rule gives.

    Every distinct condition is asked, each in its own call, up to
    ``NOVA_EVALUATION_CONCURRENCY`` at once: the calls and their verdicts do
    not depend on one another, so asking them side by side changes nothing but
    the wait. Only the opt-in ``NOVA_LLM_MAX_CONDITIONS`` leaves some unasked
    (:func:`_conditions_to_ask`), the conditions in *ask_first* kept first.
    """
    cost = cost if cost is not None else _EvaluationCost()
    prompts: set[tuple[str, float | None]] = set()
    for rule in rules:
        for pattern in (getattr(rule, "llms", None) or {}).values():
            text = _pattern_text(pattern)
            if text:
                prompts.add(nova_matching.llm_verdict_key(text, getattr(pattern, "threshold", None)))
    if not prompts:
        return {}

    from app.llm.base import ChatMessage
    from app.llm.router import get_llm_for_user

    try:
        llm, resolved = await get_llm_for_user(db, user_id, model_override=model)
    except Exception:
        logger.info("No LLM provider available — llm tier inert", exc_info=True)
        return {}

    asked = _conditions_to_ask(prompts, ask_first, cost)

    async def _judge(key: tuple[str, float | None]) -> bool | None:
        template, temperature = key
        cost.llm_calls += 1
        try:
            response = await llm.chat(
                messages=[
                    ChatMessage(role="system", content=_LLM_SYSTEM_PROMPT),
                    ChatMessage(
                        role="user",
                        content=f"Condition: {template}\n\nMessage:\n{prompt}",
                    ),
                ],
                max_tokens=_LLM_MAX_TOKENS,
                model=resolved,
                **({} if temperature is None else {"temperature": temperature}),
            )
        except Exception:
            logger.info("Nova llm tier call failed", exc_info=True)
            return None
        answer = (getattr(response, "content", "") or "").strip().lower()
        return answer.startswith("yes")

    judged = await _run_bounded(
        [functools.partial(_judge, key) for key in asked], _evaluation_concurrency()
    )
    return {key: verdict for key, verdict in zip(asked, judged, strict=True) if verdict is not None}


def _llm_condition_ceiling() -> int:
    """``NOVA_LLM_MAX_CONDITIONS``: ``0`` (every condition) when it cannot be read."""
    try:
        return max(0, int(_config.settings.nova_llm_max_conditions))
    except (AttributeError, TypeError, ValueError):
        return 0


def _condition_order(key: tuple[str, float | None]) -> tuple[str, bool, float]:
    """A total order over ``(condition, temperature)``: no temperature sorts first."""
    template, temperature = key
    return (template, temperature is not None, temperature or 0.0)


def _conditions_to_ask(
    prompts: set[tuple[str, float | None]],
    ask_first: Iterable[tuple[str, float | None]],
    cost: _EvaluationCost,
) -> list[tuple[str, float | None]]:
    """The conditions this evaluation asks: all of them, unless a ceiling is set.

    Under a ceiling the conditions of *ask_first* (those of ``block`` rules)
    come first, each group in text order, so the same ones are asked every
    time; the rest are counted and reported once per rule set.
    """
    ordered = sorted(prompts, key=_condition_order)
    cost.llm_conditions = len(ordered)
    ceiling = _llm_condition_ceiling()
    if not ceiling or len(ordered) <= ceiling:
        return ordered
    first = set(ask_first)
    ordered.sort(key=lambda key: key not in first)
    skipped = len(ordered) - ceiling
    cost.llm_conditions_skipped = skipped
    _report_skipped_conditions(ordered, ceiling, skipped)
    return ordered[:ceiling]


def _report_skipped_conditions(
    ordered: list[tuple[str, float | None]], ceiling: int, skipped: int
) -> None:
    """Warn once per rule set (its conditions and the ceiling) about what goes unasked."""
    conditions = sorted(f"{template}\x00{temperature}" for template, temperature in ordered)
    digest = hashlib.sha256("\n".join([str(ceiling), *conditions]).encode()).hexdigest()
    if digest in _reported_skips:
        return
    if len(_reported_skips) >= _REPORTED_SKIPS_MAX:
        _reported_skips.clear()
    _reported_skips.add(digest)
    logger.warning(
        "Nova llm tier: NOVA_LLM_MAX_CONDITIONS=%d asks %d of the %d llm: conditions of "
        "the enabled rules; the other %d are not asked, so a rule resting on one cannot "
        "fire through that tier (the conditions of block rules are asked first)",
        ceiling,
        ceiling,
        len(ordered),
        skipped,
        extra={
            "nova_llm_max_conditions": ceiling,
            "nova_llm_conditions": len(ordered),
            "nova_llm_conditions_skipped": skipped,
        },
    )
