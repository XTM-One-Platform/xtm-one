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

"""Quality metric seam onto ``evaluation_metrics.quality``.

The only place the engine obtains deterministic quality metrics. It resolves the stored
``Evaluation.family`` string to the package ``Family`` enum, builds the package
``FamilyConfig``, shapes the reference/prediction to what each family expects, runs
``run_for_family``, and maps every package ``ScoreResult`` onto a platform-neutral
``MetricOutcome``. Package-native types (``Family``, ``ScoreResult``, ``FamilyConfig``, the
STIX backends) never cross into an engine, API or serializer signature.

Reference/prediction shaping per family:

* ``STRUCTURED_JSON``: both pass through as strings; the package parses them itself. The
  runner requires a schema, so with none the schema-dependent metrics do not contribute,
  which is not an error.
* ``CLASSIFICATION``: both are parsed into label sets and scored as a dataset-of-one
  against a vocabulary derived once from the dataset references.
* ``STIX_BUNDLE``: the reference (ground truth) is parsed to a bundle dict; a malformed
  reference is caller misconfiguration and raises :class:`QualityConfigError`. The
  prediction is parsed when it is valid JSON and otherwise passed through so the package's
  envelope check scores it 0. STIX needs ``pycti`` and ``stix2``, both required
  dependencies, and ``pycti`` needs the ``libmagic`` system library; when they
  cannot load, the family degrades to a single ``unavailable`` outcome, never a crashed
  run.
* ``PROSE``: both pass through as strings. The suite refuses to invent a normalization
  policy, so this adapter supplies :data:`PROSE_NORMALIZATION_POLICY`.

The metrics in ``registry.HIDDEN_QUALITY_KEYS`` are computed by the package and dropped
here, so no run stores them.

An unknown family id raises :class:`QualityConfigError` rather than degrading quietly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

# Suite types are adapter-internal and must not be re-exported past this package
# (tests/test_evaluation_guards.py).
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality import (
    CaseMode,
    ComparisonPolicy,
    FamilyConfig,
    ProseConfig,
    ProseNormalizationPolicy,
    PunctuationMode,
    ScoreResult,
    UnicodeForm,
    WhitespaceMode,
    run_for_family,
    trim_edge_marks,
    unreadable_answer,
)
from app.evaluation_metrics.quality.families.structured_json.pointer import (
    pointer_from_tokens as _pointer_from_tokens,
)
from app.services.evaluation.metric_package.families import (
    CLASSIFICATION,
    PROSE,
    STIX_BUNDLE,
    STRUCTURED_JSON,
    resolve_family,
)
from app.services.evaluation.metric_package.registry import HIDDEN_QUALITY_KEYS
from app.services.evaluation.metric_package.results import MetricOutcome, MetricStatus
from app.services.structured_output import extract_json_text

_LABEL_SPLIT_RE = re.compile(r"[,\n;]")

#: A Markdown code fence line, as CommonMark reads one: up to three spaces, three
#: or more backticks or tildes, then any info string (``json``, `` json``,
#: ``json title=x``), which after backticks holds no backtick. Markup, never a label.
#: Not ``structured_output._MD_FENCE_RE``, the lenient reader behind ``find_json``: that one
#: finds JSON anywhere in an answer, this one unwraps a whole answer whose content may be a
#: plain list ``find_json`` refuses.
_FENCE_LINE_RE = re.compile(r"\A {0,3}(?P<fence>`{3,}(?=[^`]*\Z)|~{3,}).*\Z")


def _closes_fence(line: str, char: str, length: int) -> bool:
    """Whether ``line`` closes a fence of ``length`` ``char``: the same character, at least
    as many, nothing else but up to three leading spaces."""
    stripped = line.strip()
    return (
        len(stripped) >= length
        and set(stripped) == {char}
        and len(line) - len(line.lstrip(" ")) <= 3
    )


def _code_block_content(raw: str) -> str | None:
    """What ``raw`` holds when the whole of it is ONE Markdown code block, else ``None``.

    An agent told to answer in Markdown fences the list it was asked for. Its first
    line opens a fence, its last line closes it, and no line between closes it first: two
    blocks with prose between them are not one block. A closer glued to the last line of
    content (``["a"]`````) closes it too: not CommonMark, but how models write it.
    """
    lines = raw.splitlines()
    opener = _FENCE_LINE_RE.match(lines[0]) if len(lines) >= 2 else None
    if opener is None:
        return None
    fence = opener.group("fence")
    char, length = fence[0], len(fence)
    body = lines[1:-1]
    if any(_closes_fence(line, char, length) for line in body):
        return None
    last = lines[-1].rstrip()
    if _closes_fence(last, char, length):
        return "\n".join(body)
    content = last.rstrip(char)
    if len(last) - len(content) < length or not content.strip():
        return None
    return "\n".join([*body, content])


#: The platform's prose normalization policy, one choice platform-wide.
#:
#: The suite gives ``ProseConfig.policy`` no default, because normalization changes what
#: every prose score means. It is NOT configurable per evaluation: two evaluations
#: normalizing differently would produce scores that look comparable and are not.
#:
#: Two axes depart from the suite's defaults. ``casefold``: casing is noise in free text,
#: and preserving it turns exact match into a formatting check. Punctuation is trimmed at
#: the edges of each token (``edges``): one trailing period took token F1 from 100
#: to 66.7, and markdown bold to 33.3. It is NOT stripped inside a token: in CTI content it
#: is load-bearing (hashes, domains, file paths, rule ids), and stripping it would split
#: compound identifiers into separate tokens.
#:
#: Changing any axis changes what a metric means and MUST bump
#: ``app.evaluation_metrics.__version__``, or a score moved by the rules is
#: indistinguishable from an agent regression. ``tests/test_evaluation_metric_adapters.py``
#: pins all four axes.
#:
#: A fifth axis is not expressed here: the suite tokenizes on whitespace. A script without
#: spaces between words (Chinese, Japanese, Thai) yields one token per sentence, collapsing
#: ``token_prf`` and ``rouge_l`` into ``exact_match``. Each result's ``detail`` carries
#: ``reference_tokens`` / ``prediction_tokens`` (a 1 against a long input is the
#: signature), so no extra flag is emitted.
PROSE_NORMALIZATION_POLICY: Final[ProseNormalizationPolicy] = ProseNormalizationPolicy(
    unicode_form=UnicodeForm.NFC,
    case=CaseMode.casefold,
    punctuation=PunctuationMode.edges,
    whitespace=WhitespaceMode.collapse,
)


class QualityConfigError(Exception):
    """A platform configuration error surfaced from the quality seam.

    Raised for a missing/underivable vocabulary, a malformed ground-truth reference, or a
    package ``ConfigurationError``. The caller turns it into a 4xx; it is never persisted
    as a metric outcome, so a configuration mistake cannot masquerade as a score.
    """


@dataclass(frozen=True)
class QualityConfig:
    """Platform-neutral, family-scoped quality inputs, built once per run.

    The engine holds this opaquely (produced by :func:`build_quality_config`, consumed by
    :func:`run_quality`) and never inspects its fields. The STIX backend objects are package
    instances kept adapter-internal.
    """

    family: str
    vocabulary: frozenset[str] | None = None
    stix_canonicalizer: Any | None = None
    stix_validator: Any | None = None
    stix_unavailable_reason: str | None = None
    # The ``ProseNormalizationPolicy``, or None for classification and STIX. The policy,
    # not a built ``ProseConfig``: the suite type is built in ``_build_family_config``.
    prose_policy: Any | None = None
    # Per-field scoring intent declared on the evaluation, keyed by JSON Pointer. Empty for
    # every family but the JSON one, the only one scored field by field.
    field_modes: Mapping[str, str] | None = None
    field_weights: Mapping[str, float] | None = None


# --- Config construction ---------------------------------------------------------------


def build_quality_config(
    family: str,
    evaluation: Any,
    dataset_refs: Sequence[str],
    declared_vocabulary: frozenset[str] | None = None,
) -> QualityConfig:
    """Derive the family-scoped quality config once per run.

    * ``CLASSIFICATION``: ``vocabulary`` is ``declared_vocabulary``, normalized like every
      label (:func:`normalize_label`), when the dataset declares one, so a predicted label inside it but absent from an
      entry's reference scores as a false positive rather than invalidating the run.
      Otherwise it is the observed union of dataset reference labels
      (``_derive_vocabulary``); an empty fallback vocabulary is a configuration error,
      never a silent zero.
    * ``STIX_BUNDLE``: construct the package's default ``OpenCtiCanonicalizer`` +
      ``StandardsStixValidator``; a backend that cannot load is recorded so the run
      degrades to ``unavailable`` rather than crashing.
    * ``PROSE``: carry the platform's fixed normalization policy, which the suite requires.
    * structured-output families: carry the prose policy and the per-field scoring
      overrides.

    Raises:
        QualityConfigError: the family is unknown/unset, or required config is underivable.
    """
    try:
        resolve_family(family)
    except ValueError as exc:
        raise QualityConfigError(str(exc)) from exc

    if family == CLASSIFICATION:
        return QualityConfig(
            family=family,
            vocabulary=(
                _labels(declared_vocabulary)
                if declared_vocabulary
                else _derive_vocabulary(dataset_refs)
            ),
        )

    if family == STIX_BUNDLE:
        canonicalizer, validator, unavailable = _build_stix_backends()
        return QualityConfig(
            family=family,
            stix_canonicalizer=canonicalizer,
            stix_validator=validator,
            stix_unavailable_reason=unavailable,
        )

    if family == PROSE:
        return QualityConfig(
            family=family,
            prose_policy=PROSE_NORMALIZATION_POLICY,
        )

    modes, weights = _field_scoring_overrides(evaluation)

    # The SAME prose policy instance PROSE uses: a free_text field is scored by the same
    # primitive as a whole-answer prose metric, so the two are comparable only under one
    # policy. An equivalent copy would drift the first time either is edited alone.
    return QualityConfig(
        family=family,
        prose_policy=PROSE_NORMALIZATION_POLICY,
        field_modes=modes,
        field_weights=weights,
    )


def _field_scoring_overrides(
    evaluation: Any,
) -> tuple[dict[str, str], dict[str, float]]:
    """Read the evaluation's per-field scoring intent.

    Shape on the model, both keyed by JSON Pointer::

        {"field_modes": {"/severity": "exact"}, "field_weights": {"/summary": 2.0}}

    Read defensively: the column is free-form JSON, so a hand-edited or old row can hold
    anything. A malformed value is dropped so the run proceeds under derived treatment; a
    well-formed but invalid one (an unknown mode, a negative weight) is left for
    ``ComparisonPolicy`` to refuse loudly, since the API rejects those at write time and
    reaching one at run time means something bypassed it.
    """
    raw = getattr(evaluation, "field_scoring_overrides", None)
    if not isinstance(raw, Mapping):
        return {}, {}
    raw_modes = raw.get("field_modes")
    raw_weights = raw.get("field_weights")
    modes = (
        {str(k): str(v) for k, v in raw_modes.items()}
        if isinstance(raw_modes, Mapping)
        else {}
    )
    weights = (
        {
            str(k): float(v)
            for k, v in raw_weights.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if isinstance(raw_weights, Mapping)
        else {}
    )
    return modes, weights


def _derive_vocabulary(dataset_refs: Sequence[str]) -> frozenset[str]:
    """Union of labels observed across the dataset reference answers."""
    labels: set[str] = set()
    for ref in dataset_refs:
        labels |= parse_labels(ref)
    if not labels:
        raise QualityConfigError(
            "CLASSIFICATION vocabulary could not be derived: no labels were found in the "
            "dataset reference answers. Provide reference labels (a JSON array or a "
            "comma-separated list) for each entry."
        )
    return frozenset(labels)


def resolve_declared_vocabulary(
    entry_vocabularies: Sequence[Sequence[str] | None],
    entry_refs: Sequence[str],
) -> frozenset[str] | None:
    """Union declared CLASSIFICATION vocabularies across a run's entries.

    An evaluation can link several datasets, and each entry carries its own dataset's
    declared vocabulary (or ``None``/empty). Taking any one declaration for the whole run
    would drop labels declared by the other datasets and invalidate entries of a dataset
    that declared none. So every declared vocabulary is unioned, plus, for entries whose
    dataset declared none, the vocabulary derived from their own reference answers only.

    Returns ``None`` (never raises) when no entry declares any vocabulary, so callers fall
    back to ``build_quality_config``'s full-run derivation.
    """
    declared: set[str] = set()
    missing_refs: list[str] = []
    for vocab, ref in zip(entry_vocabularies, entry_refs, strict=True):
        if vocab:
            declared.update(_labels(vocab))
        else:
            missing_refs.append(ref)

    if not declared:
        return None

    if missing_refs:
        declared.update(_derive_vocabulary(missing_refs))

    return frozenset(declared)


def _build_stix_backends() -> tuple[Any | None, Any | None, str | None]:
    """Construct the default STIX canonicalizer + validator, or report unavailability.

    ``pycti`` and ``stix2`` are required dependencies, but ``pycti`` also needs the
    ``libmagic`` system library, which an image can lack. When either cannot load,
    the import raises and STIX quality degrades to ``unavailable``, never a failed run.
    """
    try:
        from app.evaluation_metrics.quality.backends.stix_opencti import OpenCtiCanonicalizer
        from app.evaluation_metrics.quality.backends.stix_validator import (
            StandardsStixValidator,
        )

        return OpenCtiCanonicalizer(), StandardsStixValidator(), None
    except Exception as exc:  # any import/construction failure degrades to unavailable
        return None, None, f"STIX backend unavailable: {exc}"


# --- Execution -------------------------------------------------------------------------


def run_quality(
    *,
    family: str,
    reference: object,
    prediction: str,
    output_schema: Mapping[str, Any] | None,
    config: QualityConfig,
) -> list[MetricOutcome]:
    """Compute the family's deterministic quality metrics for one entry.

    Returns a list of platform-neutral :class:`MetricOutcome`; no package ``ScoreResult`` or
    ``Family`` crosses this boundary. Raises :class:`QualityConfigError` (never a score) for
    a package ``ConfigurationError`` or a malformed ground-truth reference.
    """
    try:
        resolved = resolve_family(family)
    except ValueError as exc:
        raise QualityConfigError(str(exc)) from exc

    # The schema is whatever the caller resolved from the entry or the dataset, with no
    # evaluation-level fallback: an absent schema means the ground truth declared none,
    # which is reported below rather than papered over.
    effective_schema = _json_schema_definition(output_schema)

    # STRUCTURED_JSON scores fields declared by a schema, so with none there is nothing to
    # score. It is stated as `unavailable` so an unconfigured dataset does not look like a
    # broken one, and not `invalid` or `0`: the omission is the author's, not the
    # answer's.
    if family == STRUCTURED_JSON and effective_schema is None:
        return [
            _unavailable_outcome(
                "json.field_accuracy",
                "No answer shape declared. Set the dataset's expected answer schema "
                "(Dataset > Output schema), or declare one on the individual entry.",
            )
        ]

    if family == STIX_BUNDLE and config.stix_unavailable_reason:
        return [_unavailable_outcome("stix.backend", config.stix_unavailable_reason)]

    shaped_reference, shaped_prediction = _shape_inputs(family, reference, prediction)

    try:
        # Inside the ``try``: stored overrides the policy now refuses are a configuration
        # error, never an entry that failed.
        fam_config = _build_family_config(family, effective_schema, config)
        results = run_for_family(shaped_reference, shaped_prediction, resolved, fam_config)
    except ConfigurationError as exc:
        raise QualityConfigError(str(exc)) from exc

    # Hidden metrics are computed, then dropped here on purpose: the package contract stays
    # unchanged for now, and the phrase metrics cost nothing (they return `skipped`).
    outcomes = [
        _to_outcome(key, result)
        for key, result in results.items()
        if key not in HIDDEN_QUALITY_KEYS
    ]
    hint = WRAPPED_JSON_HINTS.get(family)
    # Searched only when an answer scored 0 as not well formed, never on every entry.
    if hint and any(map(_not_well_formed, outcomes)) and _holds_wrapped_json(prediction):
        outcomes = [_with_hint(outcome, hint) if _not_well_formed(outcome) else outcome for outcome in outcomes]
    return outcomes


#: Added to the reason of a 0 the answer's wrapping caused: scoring stays strict,
#: since a consumer of the agent's answer parses it strictly too, but says why and what to
#: change. Only the JSON family can extract its answer (``extraction.spec``).
WRAPPED_JSON_HINTS: Final[Mapping[str, str]] = {
    STRUCTURED_JSON: (
        "The answer holds valid JSON inside a code fence or surrounding text, and is "
        "scored as written. Declare the agent's output format as JSON, or start its "
        "extractor with 'Find JSON in text'."
    ),
    STIX_BUNDLE: (
        "The answer holds a valid JSON bundle inside a code fence or surrounding text, and "
        "is scored as written. Declare the agent's output format as JSON."
    ),
}


def _not_well_formed(outcome: MetricOutcome) -> bool:
    detail = outcome.serialized.get("detail")
    return (
        outcome.status is MetricStatus.SCORED
        and outcome.value == 0.0
        and isinstance(detail, dict)
        and detail.get("reason") == "prediction_not_well_formed"
    )


def _holds_wrapped_json(prediction: object) -> bool:
    """True iff ``prediction`` is not JSON itself but wraps a JSON object or array.

    A bare JSON answer is never "wrapped", even when a string inside it holds a fence.
    """
    if not isinstance(prediction, str):
        return False
    try:
        json.loads(prediction)
        return False
    except ValueError:
        pass
    found = extract_json_text(prediction)
    if found == prediction.strip():
        return False
    try:
        return isinstance(json.loads(found), (dict, list))
    except ValueError:
        return False


def _with_hint(outcome: MetricOutcome, hint: str) -> MetricOutcome:
    detail = outcome.serialized["detail"]
    return replace(
        outcome,
        detail=f"{outcome.detail} {hint}" if outcome.detail else hint,
        serialized={**outcome.serialized, "detail": {**detail, "hint": hint}},
    )


def _json_schema_definition(output_schema: object) -> Mapping[str, Any] | None:
    """Unwrap a stored ``output_schema`` into the bare JSON Schema the package expects.

    The platform stores an answer shape as a tagged wrapper,
    ``{"type": "json_schema", "definition": {...}}``, because ``regex`` and
    ``tool_sequence`` shapes share the column. The package takes a plain JSON Schema: the
    wrapper would make ``jsonschema`` read ``"json_schema"`` as an invalid type keyword and
    raise ``ConfigurationError`` on every entry.

    A non-``json_schema`` wrapper returns ``None`` rather than an unusable definition:
    a regex shape describes an answer this family cannot score field by field, and
    ``run_quality`` already reports that absence as ``unavailable`` with a reason.
    """
    if not isinstance(output_schema, Mapping):
        return None
    if "definition" not in output_schema:
        # A bare JSON Schema, passed by a caller that never wrapped it.
        return output_schema
    if output_schema.get("type") != "json_schema":
        return None
    definition = output_schema.get("definition")
    return definition if isinstance(definition, Mapping) else None


def _build_family_config(
    family: str, schema: Mapping[str, Any] | None, config: QualityConfig
) -> FamilyConfig:
    """Build the package ``FamilyConfig`` for ``family`` (adapter-internal)."""
    if family == CLASSIFICATION:
        return FamilyConfig(vocabulary=config.vocabulary)
    if family == STIX_BUNDLE:
        return FamilyConfig(
            canonicalizer=config.stix_canonicalizer,
            stix_validator=config.stix_validator,
        )
    if family == STRUCTURED_JSON:
        # Reached only when a schema is present (guarded in run_quality).
        return FamilyConfig(
            json_schema=schema,
            comparison_policy=_build_comparison_policy(config),
        )
    if family == PROSE:
        # Phrase lists stay unset: no evaluation can configure them. The phrase
        # metrics compute `skipped`, and `run_quality` drops them (`HIDDEN_QUALITY_KEYS`).
        return FamilyConfig(prose=ProseConfig(policy=config.prose_policy))
    # Unreachable: `run_quality` resolves the family first, and `run_for_family` raises
    # for a family it has no runner for.
    return FamilyConfig()


def _build_comparison_policy(config: QualityConfig) -> ComparisonPolicy:
    """Build the per-field policy the JSON metric is scored under (adapter-internal).

    Without it, ``routing._run_structured_json`` falls back to the strict default and no
    configured override reaches the metric.

    Args:
        config: The per-run quality config carrying the author's scoring intent.

    Returns:
        A :class:`ComparisonPolicy` carrying the prose policy every ``free_text`` field is
        scored under, plus any per-field overrides the evaluation declared.
    """
    return ComparisonPolicy(
        prose_policy=config.prose_policy,
        field_modes=dict(config.field_modes or {}),
        field_weights=dict(config.field_weights or {}),
    )


def _shape_inputs(
    family: str, reference: object, prediction: str
) -> tuple[object, object]:
    """Coerce reference/prediction into the shape ``family`` expects."""
    if family == CLASSIFICATION:
        return [parse_labels(reference)], [parse_labels(prediction)]
    if family == STIX_BUNDLE:
        return _parse_stix_reference(reference), _parse_stix_prediction(prediction)
    # STRUCTURED_JSON parses its own strings; PROSE takes text as-is (the suite normalizes
    # it per the configured policy).
    return reference, prediction


# --- Persistence -----------------------------------------------------------------------


def to_persistable(outcomes: list[MetricOutcome]) -> dict[str, dict]:
    """Map outcomes to the ``metric_results`` blob: ``{metric_key: serialized}``.

    The serialized dict is the package's own byte-stable ``ScoreResult.to_dict()``; the
    engine persists it verbatim so re-runs are byte-stable.
    """
    return {outcome.key: outcome.serialized for outcome in outcomes}


# --- Mapping + parsing helpers ---------------------------------------------------------


def _to_outcome(key: str, result: ScoreResult) -> MetricOutcome:
    """Map one package ``ScoreResult`` onto a platform-neutral ``MetricOutcome``."""
    return MetricOutcome(
        key=key,
        status=MetricStatus(result.status.value),
        value=result.value,
        detail=_detail_text(result),
        serialized=result.to_dict(),
    )


def unscorable_outcomes(family: str, reason: str, message: str) -> list[MetricOutcome]:
    """Every quality metric *family* stores, left out (``unavailable``) with the *reason*
    code and its *message*: an entry a configuration problem or a limit of ours keeps from
    being scored."""
    from app.services.evaluation.metric_package.catalog import quality_catalog

    return [
        MetricOutcome(
            key=metric["key"],
            status=MetricStatus.UNAVAILABLE,
            value=None,
            detail=message,
            serialized={
                "status": MetricStatus.UNAVAILABLE.value,
                "value": None,
                "detail": {"reason": reason, "message": message},
                "metadata": {"key": metric["key"]},
            },
        )
        for metric in quality_catalog(family)
    ]


def failed_answer_outcomes(
    scorable: list[MetricOutcome], reason: str, message: str
) -> list[MetricOutcome]:
    """*scorable*, the entry's outcomes for the expected answer, with every ``scored``
    metric scored 0 with the *reason* code and its *message*: the agent's answer cannot be
    scored. It counts in the run as the expected answer would, by the package's
    rule for an answer it cannot read (``unreadable_answer``); one the reference is
    too large to pair with itself weighs one item. The others keep their status: a metric
    this entry's configuration leaves out is left out whatever the answer.
    """
    return [
        replace(
            _to_outcome(
                outcome.key,
                unreadable_answer(
                    ScoreResult.model_validate(outcome.serialized),
                    {"reason": reason, "message": message},
                    limited=not scored,
                ),
            ),
            detail=message,
        )
        if (scored := outcome.status is MetricStatus.SCORED) or _limited(outcome)
        else outcome
        for outcome in scorable
    ]


def _limited(outcome: MetricOutcome) -> bool:
    """Whether only a compute limit on the reference kept *outcome* from scoring."""
    detail = outcome.serialized.get("detail")
    return (
        outcome.status is MetricStatus.SKIPPED
        and isinstance(detail, Mapping)
        and detail.get("reason") == "pairing_input_too_large"
    )


def _unavailable_outcome(key: str, reason: str) -> MetricOutcome:
    """A synthetic ``unavailable`` outcome: a STIX backend that cannot load, or no schema."""
    return MetricOutcome(
        key=key,
        status=MetricStatus.UNAVAILABLE,
        value=None,
        detail=reason,
        serialized={
            "status": MetricStatus.UNAVAILABLE.value,
            "value": None,
            "detail": {"reason": reason},
            "metadata": {"key": key},
        },
    )


def _detail_text(result: ScoreResult) -> str | None:
    """Extract a human-readable explanation from a package result.

    Prefers ``detail["error_message"]`` (the real parse/violation message, e.g.
    "3 schema violation(s)") over ``detail["reason"]`` (a stable machine code like
    ``schema_violation``): structured_json's validity gate carries both, and the code
    alone is not what a user reading the run should see.
    """
    if result.error is not None:
        return result.error.message
    detail = result.detail
    if isinstance(detail, Mapping):
        error_message = detail.get("error_message")
        if isinstance(error_message, str):
            return error_message
        reason = detail.get("reason")
        if isinstance(reason, str):
            return reason
    return None


#: How a CLASSIFICATION label compares: Unicode-normalized, case-folded, whitespace
#: collapsed, then the marks that present it trimmed at the ends of the whole label (the
#: prose ``edges`` rule), so ``Phishing``, ``phishing.`` and `` `phishing` `` are one
#: label while ``C#`` and ``C`` stay two, and ``phishing (high confidence)`` keeps its
#: inner parentheses. Its own constant, not the prose policy's: a change to how prose
#: scores must not silently change which labels match.
_LABEL_TEXT_POLICY: Final[ProseNormalizationPolicy] = ProseNormalizationPolicy(
    unicode_form=UnicodeForm.NFC,
    case=CaseMode.casefold,
    punctuation=PunctuationMode.preserve,
    whitespace=WhitespaceMode.collapse,
)


def normalize_label(label: object) -> str:
    """One label as it compares; empty when it holds nothing but marks."""
    return trim_edge_marks(_LABEL_TEXT_POLICY.normalize(str(label)))


def _labels(values: Iterable[object]) -> frozenset[str]:
    return frozenset(label for value in values if (label := normalize_label(value)))


def parse_labels(text: object) -> frozenset[str]:
    """Parse a classification reference or prediction into a set of normalized labels.

    The one parser for scoring and for the dataset write-time check
    (``scorers.classification_vocabulary``), so an author's input parses as it is
    scored. Accepts a JSON array (``["a","b"]``), a JSON string (a single label), a
    native list/set, or a comma/semicolon/newline-separated list. An answer that is one
    Markdown code block is read as what the block holds, and a fence line is never a
    label. Labels are normalized (:func:`normalize_label`); blank ones are
    dropped. Any other text is split as a list: ``The answer is A, B.`` gives
    ``the answer is a`` and ``b``, a wrong label and a right one, which an extractor
    avoids.
    """
    if text is None:
        return frozenset()
    if isinstance(text, (list, tuple, set, frozenset)):
        return _labels(text)
    raw = str(text).strip()
    if (content := _code_block_content(raw)) is not None:
        raw = content.strip()
    if not raw:
        return frozenset()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return _labels(parsed)
    if isinstance(parsed, str):
        return _labels([parsed])
    return _labels(part for part in _LABEL_SPLIT_RE.split(raw) if not _FENCE_LINE_RE.match(part.strip()))


def stix_reference_error(reference: object) -> str | None:
    """Why ``run_quality`` could not read a STIX reference, or ``None``, for a dataset write
    to refuse it. An empty one is a draft and passes."""
    if isinstance(reference, str) and not reference.strip():
        return None
    try:
        _parse_stix_reference(reference)
    except QualityConfigError as exc:
        return str(exc)
    return None


def _parse_stix_reference(reference: object) -> object:
    """Parse the STIX ground-truth reference into a bundle dict.

    The reference is caller ground truth: a non-JSON / non-object reference is caller
    misconfiguration and raises :class:`QualityConfigError`, never a score.
    """
    if isinstance(reference, Mapping):
        return reference
    if isinstance(reference, (str, bytes)):
        try:
            parsed = json.loads(reference)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise QualityConfigError(
                f"STIX_BUNDLE reference is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise QualityConfigError(
                "STIX_BUNDLE reference must be a JSON object (a STIX bundle)"
            )
        return parsed
    raise QualityConfigError(
        "STIX_BUNDLE reference must be a STIX bundle (JSON object or JSON string)"
    )


def _parse_stix_prediction(prediction: object) -> object:
    """Parse the STIX prediction when it is valid JSON, else pass it through.

    A prediction that is not a JSON object is the agent's own output; leaving it unparsed
    lets the package's envelope check score it 0 instead of raising.
    """
    if isinstance(prediction, Mapping):
        return prediction
    if isinstance(prediction, (str, bytes)):
        try:
            parsed = json.loads(prediction)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return prediction
        return parsed if isinstance(parsed, Mapping) else prediction
    return prediction


#: Re-exported so platform code builds RFC 6901 pointers without a second copy of the
#: escaping rule, which would eventually disagree. `TID251` keeps `app.evaluation_metrics`
#: reachable only through this seam.
pointer_from_tokens = _pointer_from_tokens

__all__ = [
    "QualityConfig",
    "QualityConfigError",
    "build_quality_config",
    "failed_answer_outcomes",
    "normalize_field_scoring_overrides",
    "normalize_label",
    "parse_labels",
    "pointer_from_tokens",
    "run_quality",
    "stix_reference_error",
    "to_persistable",
    "unscorable_outcomes",
]


def normalize_field_scoring_overrides(overrides: dict | None) -> dict | None:
    """Collapse an override object that declares nothing to ``None``.

    ``NULL`` is the one empty state: "no overrides at all". A stored ``{}`` or
    ``{"field_modes": {}, "field_weights": null}`` scores the same, but two spellings of
    "nothing" would make every reader handle both and the response body differ between an
    evaluation created with `{}` and one updated to it.

    Call it after ``validate_field_scoring_overrides``: a malformed object is a 422, not
    something to quietly normalise into a valid-looking one.
    """
    if not isinstance(overrides, dict):
        return None
    declared = {
        key: value
        for key in ("field_modes", "field_weights")
        if (value := overrides.get(key))
    }
    return declared or None


def validate_field_scoring_overrides(overrides: dict | None) -> str | None:
    """Validate per-field JSON scoring intent for API acceptance.

    Returns an error message, or ``None`` when the configuration is acceptable.

    Delegated to ``ComparisonPolicy`` so the API never accepts a configuration the scorer
    would reject, including contradictory ones (a weight on an excluded field), which
    would otherwise surface only in a run's error column.
    """
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        return "field_scoring_overrides must be an object"
    unknown = sorted(set(overrides) - {"field_modes", "field_weights"})
    if unknown:
        return (
            f"unknown field_scoring_overrides keys: {unknown}; "
            "expected 'field_modes' and/or 'field_weights'"
        )
    for key in ("field_modes", "field_weights"):
        value = overrides.get(key)
        if value is not None and not isinstance(value, dict):
            return f"field_scoring_overrides.{key} must be an object keyed by JSON Pointer"
    weights = overrides.get("field_weights") or {}
    for pointer, weight in weights.items():
        if isinstance(weight, bool) or not isinstance(weight, int | float):
            return f"field_weight at {pointer!r} must be a number"
    try:
        ComparisonPolicy(
            field_modes=dict(overrides.get("field_modes") or {}),
            field_weights={str(k): float(v) for k, v in weights.items()},
            # A ``free_text`` override is legal only alongside a prose policy, and every
            # structured-JSON run carries one: the write-time check asks what the run will.
            prose_policy=PROSE_NORMALIZATION_POLICY,
        )
    except ConfigurationError as exc:
        return str(exc)
    return None
