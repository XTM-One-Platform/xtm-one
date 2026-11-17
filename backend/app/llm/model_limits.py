# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for per-model token-limit resolution.

Two distinct limits live here, and conflating them is the mistake this module
exists to prevent:

* **Max output tokens** — the cap on what one call may generate, sent to the
  provider as ``max_tokens``. :func:`resolve_effective_max_output_tokens`.
* **Context window** — total prompt capacity, never sent anywhere; it is the
  denominator the agentic loop budgets its compaction against and the chat
  composer's context gauge displays. :func:`resolve_effective_context_window`.

They are unrelated numbers (a Claude model with a 200_000-token window caps
output at 64_000), and the bundled snapshot stores the output cap under
``max_tokens`` — so treating that key as a window under-reports it threefold.

Both apply the same explicit priority order (the ``{model: tokens}`` admin dict,
the snapshot field and the family table differ per limit; the shape does not):

1. **Admin per-model override** — ``LLMProviderConfig.max_output_tokens`` /
   ``.context_windows``, both configured in Settings -> AI Models.
2. **Model metadata snapshot** — precise limits for *known* models across every
   vendor, read from the bundled LiteLLM ``model_prices_and_context_window.json``
   (``max_output_tokens`` / ``max_tokens``, versus ``max_input_tokens``).
3. **Model-family prefix table** — a forward-looking safety net
   (:data:`_FAMILY_MAX_OUTPUT_TOKENS` / :data:`_FAMILY_CONTEXT_WINDOWS`) for
   models that ship *after* the snapshot.
4. **Provider flat class default** — ``BaseLLMProvider.max_output_tokens`` /
   ``.context_window``.
5. **Hard fallback** — :data:`_FALLBACK_MAX_OUTPUT_TOKENS` /
   :data:`_FALLBACK_CONTEXT_WINDOW`.

Why tier 3 exists
-----------------
The metadata snapshot inevitably lags new releases. A model missing from it used
to fall straight through to the provider's flat class default (8_192 for
Anthropic / OpenRouter / Bedrock - a Claude-3.5-era value). Modern frontier
models support 64k-128k output, and on Anthropic *and* Gemini the reasoning /
"thinking" tokens are billed against ``max_tokens``. With an 8_192 cap and a
non-trivial reasoning effort, internal reasoning consumes the whole budget, the
visible answer truncates, the (tool-free) continuation path can never emit a
tool call, and the agent degrades into "narrate the plan, never execute it".
The family table keeps a freshly released model from regressing to that stale
floor while the snapshot catches up.

The family values are CONSERVATIVE floors that every current member of the
family accepts; the precise (often higher) snapshot value still wins for any
model the snapshot lists, because tier 2 is consulted first.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Hard fallback when nothing else resolves (matches the base provider default).
_FALLBACK_MAX_OUTPUT_TOKENS = 16_384

# Hard fallback context window (matches ``BaseLLMProvider.context_window``).
_FALLBACK_CONTEXT_WINDOW = 128_000

_MODEL_METADATA_PATH = (
    Path(__file__).resolve().parents[1]
    / "agents"
    / "seed_data"
    / "model_prices_and_context_window.json"
)

# Single-entry cache for the model metadata file. Stored as a mutable dict (not
# two module-level globals) so the cache state can be mutated in-place without
# ``global`` declarations and the invariant ({"payload", "mtime_ns"} together)
# stays explicit. Invalidated automatically when the file mtime changes so
# long-lived workers pick up local updates without a restart.
_metadata_state: dict[str, Any] = {"payload": None, "mtime_ns": None}

# Forward-looking per-family output floors, probed MOST-SPECIFIC-FIRST.
#
# CONSERVATIVE floors accepted by every current member of the family (sending a
# value above a model's real limit is a hard HTTP 400, so we never guess high).
# Only reached for models absent from the metadata snapshot. The modern
# ``claude-<tier>-<gen>`` / ``gpt-<gen>`` naming schemes are distinct from legacy
# names (``claude-3-*`` etc.), so the broad family prefixes only match the
# current generations that all support the listed floor.
#
# Sources (provider docs, mid-2026):
#   OpenAI  - gpt-6.x (Astra): 128k, gpt-5.x: 128k, gpt-4.1: 32_768,
#             gpt-4o: 16_384, o-series: 100k
#   Anthropic - Claude 5 series (fable-5 / mythos-5 / opus-5 / sonnet-5): 128k,
#               opus 4.5-4.8: 64k-128k (4.0/4.1: 32k, snapshot-listed),
#               sonnet 4.6: 128k (snapshot-listed), sonnet/haiku 4.5: 64k
#   Gemini  - 2.5 / 3.x: 65_536 (note: COMBINED thinking + output budget)
#   SpaceXAI - grok-4.x / grok-build: output bounded only by the context
#              window (256k+), so 32_768 is a safe floor for every member
#   Mistral - no separate output cap: prompt + max_tokens must fit the
#             context window, and compaction lets a prompt reach 80% of it,
#             so 16_384 is what still fits beside a 128k window (Magistral,
#             40k, keeps 8_000)
_FAMILY_MAX_OUTPUT_TOKENS: tuple[tuple[str, int], ...] = (
    # --- OpenAI ---
    ("gpt-4o", 16_384),
    ("gpt-4.1", 32_768),
    ("gpt-5", 128_000),
    ("gpt-6", 128_000),
    ("o1", 100_000),
    ("o3", 100_000),
    ("o4", 100_000),
    # --- Anthropic Claude (4.x+ / 5.x, most-specific-first) ---
    # The Claude 5 generation (opus-5 / sonnet-5 / fable-5) supports 128k
    # output on the synchronous Messages API; dated variants (e.g.
    # ``claude-opus-5-20260524``) miss the snapshot and land here.
    # Opus 4.0/4.1 cap at 32k but are snapshot-listed, so the broad opus floor
    # is only reached by 4.5+ / future opus (all >= 64k). Every Fable and
    # Mythos model (5+) supports 128k; ``claude-mythos-5-1`` shipped after the
    # snapshot and lands here instead of the 8_192 provider default.
    ("claude-opus-5", 128_000),
    ("claude-sonnet-5", 128_000),
    ("claude-opus-", 64_000),
    ("claude-sonnet-", 64_000),
    ("claude-haiku-", 64_000),
    ("claude-fable-", 128_000),
    ("claude-mythos-", 128_000),
    # --- Google Gemini (max_output_tokens is a combined thinking+output cap) ---
    ("gemini-2.5", 65_536),
    ("gemini-3", 65_536),
    # --- SpaceXAI Grok ---
    ("grok-4", 32_768),
    ("grok-build", 32_768),
    # --- Mistral ---
    ("mistral-large", 16_384),
    ("mistral-medium", 16_384),
    ("mistral-small", 16_384),
    ("ministral-", 16_384),
    ("magistral-", 8_000),
    ("codestral", 16_384),
    ("devstral", 16_384),
    ("pixtral-", 16_384),
)

# Forward-looking per-family CONTEXT WINDOWS, probed MOST-SPECIFIC-FIRST.
#
# Note the risk direction is INVERTED versus the output table above. Sending a
# max_tokens above a model's real limit is a hard HTTP 400, so those values must
# never guess high. A context window is never sent to anyone — it is a budgeting
# denominator, and guessing HIGH is the harmful direction: it makes the loop
# believe there is headroom that does not exist, so compaction fires too late
# and the provider rejects an over-long prompt instead. These are therefore
# conservative floors too, but for the opposite reason.
#
# Sources (provider docs, mid-2026):
#   Anthropic - Claude 5 series (fable-5 / mythos-5 / opus-5 / sonnet-5): 1M;
#               opus 4.6-4.8 and sonnet 4.6 are also 1M but snapshot-listed,
#               so the broad 4.x floors stay at the conservative 200k
#               (haiku 4.5 really is 200k)
#   OpenAI    - gpt-6 (Astra): 1.05M window but 922k max INPUT tokens (the
#               window includes the 128k output budget; the input cap is the
#               correct budgeting denominator, see resolver docstring below),
#               gpt-5.4/5.5/5.6: 1.05M input, gpt-5 base/5.1/5.2: 272k,
#               gpt-4.1: ~1.05M, gpt-4o: 128k, o-series: 200k
#   Gemini    - 2.5 / 3.x: ~1.05M
#   SpaceXAI  - grok-4.7/4.6/4.5 (V9 foundation): 500k, grok-4.3/4.20: 1M,
#               grok-build: 256k, grok-4 base: 256k (docs.x.ai/developers/models)
#   Mistral   - Large 3, Medium 3.5, Small 4, Codestral: 256k; earlier dated
#               releases, Ministral, Devstral, Pixtral: 128k or more;
#               Magistral: 40k (docs.mistral.ai/getting-started/models)
#
# IF YOU ARE HERE BECAUSE COMPACTION FIRES EARLIER THAN EXPECTED: a deployment
# on Anthropic's opt-in 1M long-context beta really does have more window than
# the 200k floor above, and nothing errors when we under-estimate it — the loop
# simply starts summarising older turns much sooner than it needs to. Raise it
# per model in Settings -> AI Models, which writes
# ``LLMProviderConfig.context_windows``; that override is tier 1 and beats both
# this table and the metadata snapshot.
_FAMILY_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    # --- OpenAI (most-specific-first: dotted gpt-5 generations before the
    # bare "gpt-5" floor that also matches them) ---
    ("gpt-4o", 128_000),
    ("gpt-4.1", 1_000_000),
    # mini/nano variants keep the 272k input window (snapshot-confirmed for
    # gpt-5.4-mini/-nano) — they must be probed BEFORE the broad dotted-gen
    # prefixes below, or a new dated variant missing from the snapshot would
    # be over-budgeted at 1.05M and hit provider context errors.
    ("gpt-5.4-mini", 272_000),
    ("gpt-5.4-nano", 272_000),
    ("gpt-5.5-mini", 272_000),
    ("gpt-5.5-nano", 272_000),
    ("gpt-5.6-mini", 272_000),
    ("gpt-5.6-nano", 272_000),
    ("gpt-5.4", 1_050_000),
    ("gpt-5.5", 1_050_000),
    ("gpt-5.6", 1_050_000),
    ("gpt-5", 272_000),
    ("gpt-6", 922_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    # --- Anthropic Claude (4.x+ / 5.x, most-specific-first) ---
    ("claude-opus-5", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-opus-", 200_000),
    ("claude-sonnet-", 200_000),
    ("claude-haiku-", 200_000),
    ("claude-fable-", 1_000_000),
    ("claude-mythos-", 1_000_000),
    # --- Google Gemini ---
    ("gemini-2.5", 1_000_000),
    ("gemini-3", 1_000_000),
    # --- SpaceXAI Grok (most-specific-first: dotted generations before the
    # bare "grok-4" floor that also matches them; 4.5/4.6/4.7 share the V9
    # foundation and its 500k window) ---
    ("grok-4.7", 500_000),
    ("grok-4.6", 500_000),
    ("grok-4.5", 500_000),
    ("grok-4.3", 1_000_000),
    ("grok-4.20", 1_000_000),
    ("grok-4", 256_000),
    ("grok-build", 256_000),
    # --- Mistral (the current generations and their aliases before the
    # family floors that also match the earlier 128k releases) ---
    ("mistral-large-latest", 256_000),
    ("mistral-large-2512", 256_000),
    ("mistral-large-3", 256_000),
    ("mistral-medium-latest", 256_000),
    ("mistral-medium-2604", 256_000),
    ("mistral-medium-3-5", 256_000),
    ("mistral-medium-3.5", 256_000),
    ("mistral-small-latest", 256_000),
    ("mistral-small-2603", 256_000),
    ("mistral-small-4", 256_000),
    ("codestral", 256_000),
    ("mistral-large", 128_000),
    ("mistral-medium", 128_000),
    ("mistral-small", 128_000),
    ("ministral-", 128_000),
    ("magistral-", 40_000),
    ("devstral", 128_000),
    ("pixtral-", 128_000),
)

# Bedrock cross-region inference prefixes (e.g. ``us.anthropic.claude-...``),
# the geographic profiles included (Japan ``jp.``, Australia ``au.``,
# GovCloud ``us-gov.``).
_BEDROCK_REGION_PREFIXES = (
    "us.", "eu.", "apac.", "global.", "ap.", "ca.", "sa.", "jp.", "au.", "us-gov.",
)
# Vendor dotted prefixes used by Bedrock model ids (``anthropic.claude-...``).
_VENDOR_DOT_PREFIXES = (
    "anthropic.", "amazon.", "meta.", "mistral.", "cohere.", "ai21.",
    "deepseek.", "google.",
)


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except ValueError:
            return None
    return None


def _load_model_metadata() -> dict[str, Any] | None:
    """Load the model metadata snapshot from the local seed cache (mtime-cached)."""
    state = _metadata_state
    try:
        if not _MODEL_METADATA_PATH.exists():
            logger.debug("Model metadata file not found at %s", _MODEL_METADATA_PATH)
            state["payload"] = None
            state["mtime_ns"] = None
            return None
        stat = _MODEL_METADATA_PATH.stat()
        current_mtime_ns = stat.st_mtime_ns
        if state["payload"] is not None and state["mtime_ns"] == current_mtime_ns:
            return state["payload"]

        payload = json.loads(_MODEL_METADATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            logger.warning(
                "Model metadata file is not a JSON object: %s", _MODEL_METADATA_PATH
            )
            state["payload"] = None
            state["mtime_ns"] = current_mtime_ns
            return None
        state["payload"] = payload
        state["mtime_ns"] = current_mtime_ns
        logger.debug(
            "Loaded model metadata cache (%d models) from %s",
            len(payload),
            _MODEL_METADATA_PATH,
        )
        return state["payload"]
    except Exception:
        logger.exception("Failed to load model metadata file: %s", _MODEL_METADATA_PATH)
        return None


def _snapshot_candidate_keys(model: str) -> list[str]:
    """Snapshot keys to probe for *model*, most specific first.

    Model ids reach us in several dialects for the same underlying model, so a
    single exact lookup misses constantly:

    * aggregator / vendor prefixes — ``openrouter/anthropic/claude-...``, both
      the first-segment-stripped form (``anthropic/claude-...``) and the bare
      trailing segment (``claude-...``) are probed;
    * trailing version / variant suffixes after ``:`` — Bedrock ids end with
      ``:0``, OpenRouter variants with ``:free`` / ``:beta``. The HEAD is kept
      (the base model id), NOT the useless suffix.

    Deduplicated, order preserved, so a caller can stop at the first hit.
    """
    candidates: list[str] = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
        candidates.append(model.rsplit("/", 1)[1])
    for cand in list(candidates):
        if ":" in cand:
            candidates.append(cand.split(":", 1)[0])

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _resolve_snapshot_field(model: str | None, fields: tuple[str, ...]) -> int | None:
    """First positive int found at *fields* for *model* in the metadata snapshot."""
    if not model:
        return None

    payload = _load_model_metadata()
    if not payload:
        return None

    for key in _snapshot_candidate_keys(model):
        entry = payload.get(key)
        if not isinstance(entry, dict):
            continue
        for field in fields:
            resolved = _coerce_positive_int(entry.get(field))
            if resolved:
                return resolved

    return None


def resolve_snapshot_max_output_tokens(model: str | None) -> int | None:
    """Tier 2: precise per-model lookup from the metadata snapshot."""
    return _resolve_snapshot_field(model, ("max_output_tokens", "max_tokens"))


def resolve_snapshot_context_window(model: str | None) -> int | None:
    """Tier 2 of the CONTEXT-WINDOW chain: the model's prompt capacity.

    ``max_input_tokens`` only — deliberately NOT falling back to ``max_tokens``
    the way the output resolver does. In this snapshot ``max_tokens`` is the
    *output* cap (64_000 for a Claude model with a 200_000-token window), so
    accepting it here would report a window three times too small.
    """
    return _resolve_snapshot_field(model, ("max_input_tokens",))


def _normalize_family_key(model: str) -> str:
    """Strip aggregator / cross-region prefixes so a vendor-prefixed id (e.g.
    ``openrouter`` ``anthropic/claude-...`` or Bedrock ``us.anthropic.claude-...``)
    matches the bare model-family prefixes in :data:`_FAMILY_MAX_OUTPUT_TOKENS`.
    """
    key = model.strip().lower()
    # OpenRouter / Vertex "vendor/model" -> bare model id.
    if "/" in key:
        key = key.rsplit("/", 1)[1]
    # Bedrock "region.vendor.model" -> strip the leading region then vendor.
    for region in _BEDROCK_REGION_PREFIXES:
        if key.startswith(region):
            key = key[len(region):]
            break
    for vendor in _VENDOR_DOT_PREFIXES:
        if key.startswith(vendor):
            key = key[len(vendor):]
            break
    return key


def resolve_family_max_output_tokens(model: str | None) -> int | None:
    """Tier 3: forward-looking per-family floor for models absent from the snapshot."""
    if not model:
        return None
    key = _normalize_family_key(model)
    for prefix, limit in _FAMILY_MAX_OUTPUT_TOKENS:
        if key.startswith(prefix):
            return limit
    return None


def resolve_family_context_window(model: str | None) -> int | None:
    """Tier 3 of the context-window chain: per-family floor for unlisted models."""
    if not model:
        return None
    key = _normalize_family_key(model)
    for prefix, window in _FAMILY_CONTEXT_WINDOWS:
        if key.startswith(prefix):
            return window
    return None


def resolve_effective_context_window(
    *,
    model: str | None,
    overrides: dict[str, int] | None,
    provider_default: int | None,
    fallback: int = _FALLBACK_CONTEXT_WINDOW,
) -> int:
    """Resolve a model's context window (prompt capacity) in tokens.

    Same priority shape as :func:`resolve_effective_max_output_tokens`:

    1. **Admin per-model override** — ``LLMProviderConfig.context_windows``
       (Settings -> AI Models), surfaced as ``BaseLLMProvider.context_windows``.
    2. **Model metadata snapshot** — ``max_input_tokens`` for the model.
    3. **Model-family prefix table** — :data:`_FAMILY_CONTEXT_WINDOWS`.
    4. **Provider flat class default** — ``BaseLLMProvider.context_window``.
    5. **Hard fallback** — :data:`_FALLBACK_CONTEXT_WINDOW`.

    Why the snapshot must outrank the provider default
    --------------------------------------------------
    The provider classes declare a single flat window for every model they can
    reach, and for the aggregating providers that value is deliberately
    optimistic: Anthropic, Bedrock, OpenRouter and Gemini all declare
    ``1_000_000``. Consulting it before the snapshot means a Claude Sonnet 4.5
    run (real window 200_000) is budgeted against five times the context it
    actually has — the agentic loop's compaction gate then waits for 800_000
    tokens' worth of history before summarising anything, and the provider
    rejects the prompt long before that. Anything the snapshot lists is a
    precise per-model figure, so it wins.

    Always returns a positive int: every consumer divides by this.
    """
    if model and isinstance(overrides, dict):
        override_value = _coerce_positive_int(overrides.get(model))
        if override_value:
            return override_value

    snapshot_value = resolve_snapshot_context_window(model)
    if snapshot_value:
        return snapshot_value

    family_value = resolve_family_context_window(model)
    if family_value:
        return family_value

    provider_value = _coerce_positive_int(provider_default)
    if provider_value:
        return provider_value

    return fallback


def resolve_effective_max_output_tokens(
    *,
    model: str | None,
    overrides: dict[str, int] | None,
    provider_default: int | None,
    fallback: int | None = _FALLBACK_MAX_OUTPUT_TOKENS,
) -> int | None:
    """Resolve a call's ``max_tokens`` using the full priority chain.

    Shared by the agentic loop (``agent_loop._resolve_effective_max_tokens``) and
    the failover clamp (``router.FailoverLLMProvider._resolve_max_tokens_limit``)
    so both paths agree on every model.

    ``fallback`` is returned only when nothing else resolves. The agentic loop
    keeps the default (it must produce a concrete ``max_tokens``); the failover
    clamp passes ``None`` so an unconfigured provider yields "no clamp" instead
    of forcing a value onto a provider that declared no limit.
    """
    if model and isinstance(overrides, dict):
        override_value = overrides.get(model)
        if isinstance(override_value, int) and override_value > 0:
            return override_value

    snapshot_value = resolve_snapshot_max_output_tokens(model)
    if snapshot_value:
        return snapshot_value

    family_value = resolve_family_max_output_tokens(model)
    if family_value:
        return family_value

    provider_value = _coerce_positive_int(provider_default)
    if provider_value:
        return provider_value

    return fallback


def resolve_provider_max_output_tokens(provider: object, model: str | None) -> int | None:
    """The output ceiling *provider* applies to *model*, ``None`` when unknown.

    The chain of :func:`resolve_effective_max_output_tokens` fed from the
    provider (its admin overrides, its flat default, its default model when
    *model* is ``None``) with no final fallback, so an unconfigured provider
    means "no known ceiling" rather than a forced value.
    """
    return resolve_effective_max_output_tokens(
        model=model or getattr(provider, "default_model", None),
        overrides=getattr(provider, "max_output_tokens_overrides", None),
        provider_default=getattr(provider, "max_output_tokens", None),
        fallback=None,
    )


def retry_output_budget(provider: object, model: str | None, budget: int) -> int:
    """The budget for retrying an answer cut at *budget* output tokens.

    Twice *budget*, capped at the ceiling *provider* applies to *model* - a
    ``max_tokens`` above it is an HTTP 400 - and never below *budget* itself.
    """
    ceiling = resolve_provider_max_output_tokens(provider, model)
    doubled = 2 * budget
    if ceiling is not None:
        doubled = min(doubled, ceiling)
    return max(budget, doubled)


# ---------------------------------------------------------------------------
# Token counts as an administrator types them, and as a provider reports them
# ---------------------------------------------------------------------------

#: The largest context window accepted, typed or reported. Two orders of
#: magnitude above today's largest window: what is past it is a typo or a
#: hostile listing, never a model.
MAX_CONTEXT_WINDOW_TOKENS = 100_000_000

# ASCII digits only, as the frontend reads them (``\d`` alone also matches ``١٢٨``).
_TOKEN_SHORTHAND = re.compile(r"^(\d+)(?:[.,](\d+))? *([km])$", re.IGNORECASE | re.ASCII)
# Plain digits, or digits grouped by three with one separator used throughout.
_TOKEN_GROUPED = re.compile(r"^\d{1,3}([ ,_\u00a0\u202f])\d{3}(?:\1\d{3})*$", re.ASCII)
#: Digits each suffix scales by: ``1.05M`` is 1 * 10**6 + 05 * 10**4.
_TOKEN_SUFFIX_DIGITS = {"k": 3, "m": 6}
#: Longer than any count up to the bound can be typed: refused before parsing,
#: so a long input costs nothing (and never reaches ``int()``'s digit limit).
_MAX_TOKEN_TEXT_LENGTH = 32
#: Longest model id a context window may be keyed by (the provider requests'
#: ``context_windows`` and the scan); the frontend never offers a longer one.
MAX_MODEL_ID_LENGTH = 255


def is_model_id(value: Any) -> bool:
    """A key a context-window map may hold: a nonblank string of at most 255 characters."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_MODEL_ID_LENGTH


def parse_token_count(value: Any) -> int | None:
    """A token count typed as ``128000``, ``128,000``, ``128K`` or ``1.05M``.

    The shorthand is decimal, as the vendors write it: ``128K`` is 128_000 and
    ``1.05M`` is 1_050_000 (a comma is read as the decimal mark beside a
    suffix, ``1,05M``). The result is a whole number of tokens between 1 and
    :data:`MAX_CONTEXT_WINDOW_TOKENS`, or ``None`` when *value* is none of
    these - a fraction of a token (``1.0000005M``) included. The frontend
    mirrors this grammar in ``src/lib/token-count.ts``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        tokens = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        tokens = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > _MAX_TOKEN_TEXT_LENGTH:
            return None
        if match := _TOKEN_SHORTHAND.match(text):
            whole, fraction, suffix = match.groups()
            digits = _TOKEN_SUFFIX_DIGITS[suffix.lower()]
            # Exact integer arithmetic: a fraction finer than one token is
            # refused, never rounded away by a Decimal context's precision.
            fraction = (fraction or "").rstrip("0")
            if len(fraction) > digits:
                return None
            tokens = int(whole) * 10**digits + int(fraction.ljust(digits, "0"))
        elif text.isascii() and text.isdigit():
            tokens = int(text)
        elif _TOKEN_GROUPED.match(text):
            tokens = int(re.sub(r"\D", "", text))
        else:
            return None
    else:
        return None
    return tokens if 0 < tokens <= MAX_CONTEXT_WINDOW_TOKENS else None


def reported_context_window(value: Any) -> int | None:
    """A context window read from a provider's model listing, or ``None``.

    The listing is the provider's (or a gateway's, or a self-hosted server's):
    only a whole positive number within :data:`MAX_CONTEXT_WINDOW_TOKENS` is
    taken, never a string to parse.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return parse_token_count(value)


ContextWindowSource = Literal["provider", "catalog", "family"]


def known_context_window(model: str | None) -> tuple[int, ContextWindowSource] | None:
    """What the platform itself knows of *model*'s window, without asking the provider.

    Tiers 2 and 3 of :func:`resolve_effective_context_window`: the bundled
    snapshot (``catalog``), else the family table (``family``). The provider's
    flat class default is no knowledge of the model, so it is never returned.
    """
    if snapshot := resolve_snapshot_context_window(model):
        return snapshot, "catalog"
    if family := resolve_family_context_window(model):
        return family, "family"
    return None
