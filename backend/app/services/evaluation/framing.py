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

"""Autonomous evaluation framing — single source of truth.

Holds the autonomous-mode directive, non-interactive fallback prompts, and the
deterministic confirmation-seeking detector used by evaluation execution.

Shared by ``agent_chat.run_agent_chat`` (full_loop path) and ``evaluation_engine``
(llm_only path, fallback wording, anomaly detection) so the two execution paths
cannot diverge.

The directive is emitted **only** on evaluation paths, gated by the caller's
``evaluation_mode`` flag, so no chat or assignment system prompt grows.
"""

from __future__ import annotations

import re

from app.services.failure_causes import (
    FailureCause,
    FailureClassification,
    classify_run_failure,
)

# --- Autonomous-mode directive ---------------------------------------------

AUTONOMOUS_EVAL_DIRECTIVE = (
    "You are running in autonomous evaluation mode: no human is available to "
    "answer questions, so produce your final deliverable directly and never "
    "ask for confirmation or offer options to choose from."
)

# --- Non-interactive fallback prompts --------------------------------------

FILE_ONLY_FALLBACK_PROMPT = (
    "Produce your final deliverable for the attached files. Do not ask questions."
)

NO_INPUT_FALLBACK_PROMPT = (
    "Execute your standing instructions and produce your final deliverable. "
    "Do not ask questions."
)

# --- Anomaly-detection message --------------------------------------------

ANOMALY_REASONING = "Execution anomaly: agent asked for input in a non-interactive run."

# --- Anomaly subtype classification --------------------------------
# Persisted on EvaluationResult.execution_anomaly_kind. Infra noise is skipped by
# the judge and excluded from every run-level aggregate and from reuse. A real
# behavioural failure is judged and counted, but still never reused
# (fingerprint.py::is_result_reusable).
ANOMALY_KIND_TRANSIENT_ERROR = "transient_error"
ANOMALY_KIND_CONFIRMATION_SEEKING = "confirmation_seeking"
# The platform could not run the agent: excluded from every run-level aggregate
# like a transient error, and disclosed as left out of Overall Quality.
ANOMALY_KIND_PLATFORM_ERROR = "platform_error"


def build_autonomous_system_suffix() -> str:
    """Return the autonomous directive block to append to an eval system prompt.

    Callers append this to ``system_parts`` / ``system_message`` **only** when
    ``evaluation_mode`` is truthy. Never emitted in normal chat/assignment.
    """
    return AUTONOMOUS_EVAL_DIRECTIVE


def fallback_input_prompt(has_files: bool) -> str:
    """Return the non-interactive fallback instruction for an empty author prompt.

    ``FILE_ONLY_FALLBACK_PROMPT`` when the entry has file attachments, otherwise
    ``NO_INPUT_FALLBACK_PROMPT``. Only used by the evaluation engine when the
    author-written ``input_prompt`` is empty (author prompt always wins).
    """
    return FILE_ONLY_FALLBACK_PROMPT if has_files else NO_INPUT_FALLBACK_PROMPT


# Enumerated-option markers, e.g. "A)", "B.", "(a)", "1)", "option A", "option 2".
_OPTION_ENUM_RE = re.compile(
    r"(?:^|\n|\s)(?:\(?[A-Da-d]\)|[A-Da-d][.)]|option\s+[A-Da-d0-9]|\(?[1-9]\)|[1-9][.)])",
    re.IGNORECASE,
)

# Choice cues that, combined with enumerated options, indicate a request to pick.
_CHOICE_CUES = (
    "would you like",
    "which option",
    "let me know",
    "should i",
    "do you want",
    "please confirm",
    "please choose",
    "choose",
    "which one",
    "your preference",
    "how would you like",
)


def detect_confirmation_seeking(text: str | None) -> bool:
    """Deterministic, no-LLM heuristic: is the FINAL output a confirmation/option request?

    Conservative (a false positive would skip the judge): returns True only when
    the trimmed trailing output either
      (a) ends with a question mark, OR
      (b) contains an enumerated-options pattern combined with a choice cue.
    Returns False for empty/whitespace input. Pure — same input, same output.
    """
    if not text:
        return False

    # Strip trailing whitespace and common markdown emphasis/wrappers.
    stripped = text.strip()
    if not stripped:
        return False

    trailing = stripped.rstrip("*_`>)\"' \t\r\n")
    if trailing.endswith("?"):
        return True

    lowered = stripped.lower()
    return bool(_OPTION_ENUM_RE.search(stripped)) and any(cue in lowered for cue in _CHOICE_CUES)


# --- Transient execution-error anomaly --------------------------------------
#
# A transient provider failure (overload 529, rate limit 429, gateway 5xx) that
# outlives the agentic loop's retries surfaces as the agent's OUTPUT, e.g.
#   "Error: Error code: 529 - {...'overloaded_error'...}".
# The judge would score it 0 and drag the run aggregate, so it is flagged
# as an execution anomaly: judge skipped, excluded from aggregates.

# Transient HTTP statuses (aligned with ``is_retryable_openai`` vocabulary plus
# Anthropic 529 overload). 4xx other than 429 are NOT transient.
TRANSIENT_EXECUTION_STATUS = frozenset({429, 500, 502, 503, 504, 529})

# Transient signal keywords (matched only once error framing is present).
_TRANSIENT_EXECUTION_KEYWORDS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "too many requests",
    "timeout",
    "timed out",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "connection error",
    "temporarily unavailable",
)

_ERROR_CODE_RE = re.compile(r"error code:\s*(\d{3})(?!\d)", re.IGNORECASE)

# Execution modes whose output is authored by the LLM/agentic loop. Deterministic
# ``skip_llm`` outputs are never flagged.
_EXEC_ANOMALY_MODES = frozenset({"full_loop", "llm_only", "llm_only_fallback"})

EXECUTION_ERROR_REASONING = (
    "Execution anomaly: the agent run failed with a transient LLM provider error "
    "(overload, rate limit, or gateway) after retries; excluded from aggregate scores."
)

PLATFORM_ERROR_REASONING = (
    "Execution anomaly: the platform could not run the agent; excluded from aggregate "
    "scores."
)

# --- An entry whose agent run raised --------------------------------
# One rule for Judge Quality, Overall Quality and Overall Performance, by whose fault the
# run raised, read from the failure-cause classifier:
#
# - the provider's weather: a transient error, out of every headline (FR-075);
# - the request or the run's budget, which the agent's configuration shapes (its model,
#   its prompt, its tools, its iteration budget): the agent failed the entry, 0 in every
#   headline that measures the answer;
# - anything else, ours or unknown (configuration, licence, quota, a bug): left out of
#   every headline, with its reason. Scoring our failure as the agent's 0 would rank
#   agents by our incidents.
_TRANSIENT_CAUSES = frozenset(
    {
        FailureCause.PROVIDER_RATE_LIMITED,
        FailureCause.PROVIDER_OVERLOADED,
        FailureCause.PROVIDER_TIMEOUT,
        FailureCause.PROVIDER_CONNECTION_FAILED,
        FailureCause.ALL_PROVIDERS_FAILED,
    }
)
_AGENT_CAUSES = frozenset(
    {
        FailureCause.CONTEXT_OVERFLOW,
        FailureCause.REQUEST_TOO_LARGE,
        FailureCause.MODEL_REJECTED_REQUEST,
        FailureCause.MODEL_DECLINED,
        FailureCause.RUN_BUDGET_EXHAUSTED,
    }
)


def classify_raised_entry(exc: BaseException) -> tuple[str | None, FailureClassification]:
    """Whose fault an entry's raised agent run is, and the cause it was read from.

    Returns ``(anomaly_kind, classification)``: ``ANOMALY_KIND_TRANSIENT_ERROR`` or
    ``ANOMALY_KIND_PLATFORM_ERROR`` for an entry left out of every headline, ``None`` for
    one the agent failed, scored 0.
    """
    classification = classify_run_failure(exc)
    if classification.cause in _TRANSIENT_CAUSES:
        return ANOMALY_KIND_TRANSIENT_ERROR, classification
    if classification.cause in _AGENT_CAUSES:
        return None, classification
    return ANOMALY_KIND_PLATFORM_ERROR, classification


def detect_execution_error(text: str | None) -> bool:
    """Deterministic, no-LLM heuristic: is the FINAL output a transient failure?

    Conservative (a false positive would skip the judge and hide a real 0):
    returns True only when the trimmed output BOTH
      (a) has error framing — starts with ``Error:`` OR contains ``Error code:`` — AND
      (b) carries a transient signal — a transient HTTP status in an ``Error code:``
          marker, OR one of the transient keywords.
    Genuine deliverables that merely mention "overloaded"/"error" mid-text, and
    non-transient failures (``Context overflow:``, HTTP 400) return False so they
    are still judged. Returns False for empty/whitespace/None. Pure.
    """
    if not text:
        return False

    stripped = text.strip()
    if not stripped:
        return False

    lowered = stripped.lower()
    # Require the output to *start* with an error marker (the platform surfaces a
    # residual execution failure as the whole output). Merely mentioning "error
    # code:" mid-text — e.g. documentation about HTTP errors — is not framing.
    has_error_framing = lowered.startswith("error:") or lowered.startswith("error code:")
    if not has_error_framing:
        return False

    match = _ERROR_CODE_RE.search(lowered)
    if match and int(match.group(1)) in TRANSIENT_EXECUTION_STATUS:
        return True

    return any(keyword in lowered for keyword in _TRANSIENT_EXECUTION_KEYWORDS)


def classify_execution_anomaly(
    agent_output: str | None, exec_mode: str | None
) -> tuple[str | None, str | None]:
    """Shared anomaly classifier for the evaluation scoring paths.

    Returns ``(subtype, reason)``, both ``None`` when the output is not an
    anomaly. No separate boolean, so the two cannot drift: callers write
    ``subtype is not None``.

    Gated on an LLM-authored ``exec_mode`` so deterministic ``skip_llm`` outputs
    are never flagged. An output that is both a transient error and
    confirmation-seeking gets the transient-error subtype, the more actionable one.
    The fresh-scoring, reuse-candidacy and rescore paths all call it, so the
    persisted ``execution_anomaly_kind``, which decides run-level scoring
    treatment, is derived in one place.
    """
    if exec_mode not in _EXEC_ANOMALY_MODES:
        return None, None
    if detect_execution_error(agent_output):
        return ANOMALY_KIND_TRANSIENT_ERROR, EXECUTION_ERROR_REASONING
    if detect_confirmation_seeking(agent_output):
        return ANOMALY_KIND_CONFIRMATION_SEEKING, ANOMALY_REASONING
    return None, None
