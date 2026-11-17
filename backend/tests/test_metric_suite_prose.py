"""The PROSE metric family: exact match, token P/R/F1, ROUGE-L, the phrase
metrics, `ProseConfig`, error content, precedence, routing, and the byte-stable golden
fixtures.
"""

import functools
import json
import re
from collections.abc import Callable, Iterator
from itertools import product
from typing import Any

import pytest

from app.evaluation_metrics.core.contracts import MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality import (
    CaseMode,
    Family,
    FamilyConfig,
    ProseConfig,
    ProseNormalizationPolicy,
    PunctuationMode,
    UnicodeForm,
    WhitespaceMode,
    prose_exact_match,
    prose_forbidden_phrase_absence,
    prose_required_phrase_coverage,
    prose_rouge_l,
    prose_token_prf,
    run_for_family,
)
from app.evaluation_metrics.quality.families.prose import metrics as prose_metrics
from app.evaluation_metrics.quality.families.prose.metrics import _lcs_length
from app.evaluation_metrics.quality.results import ScoreResult
from tests.evaluation_helpers import FIXTURES_DIR, detail_list, detail_map

# ── prose.exact_match ──

_POLICY = ProseNormalizationPolicy()


def test_identical_after_normalization_scores_100() -> None:
    result = prose_exact_match("the actor", "the actor", _POLICY)
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert result.detail["match"] is True


def test_different_text_scores_zero() -> None:
    result = prose_exact_match("the actor", "an actor", _POLICY)
    assert result.value == 0.0
    assert result.detail["match"] is False


def test_repeated_whitespace_matches_under_the_default_policy() -> None:
    assert prose_exact_match(" a   b ", "a b", _POLICY).value == 100.0
    preserve = ProseNormalizationPolicy(whitespace=WhitespaceMode.preserve)
    assert prose_exact_match(" a   b ", "a b", preserve).value == 0.0


def test_unicode_composition_difference_matches_under_nfc() -> None:
    assert prose_exact_match("caf\u00e9", "cafe\u0301", _POLICY).value == 100.0
    nfd = ProseNormalizationPolicy(unicode_form=UnicodeForm.NFD)
    assert prose_exact_match("caf\u00e9", "cafe\u0301", nfd).value == 100.0


@pytest.mark.parametrize("metric", [prose_exact_match, prose_token_prf, prose_rouge_l])
@pytest.mark.parametrize(("reference", "prediction"), [("", ""), ("   ", ""), ("", "a b")])
def test_an_empty_reference_is_never_scored(
    metric: Callable[[object, object, ProseNormalizationPolicy], ScoreResult],
    reference: str,
    prediction: str,
) -> None:
    """An empty expected answer is a draft, a configuration problem: whatever the
    answer, the metric is ``skipped`` with its reason, never a score for the agent."""
    result = metric(reference, prediction, _POLICY)
    assert result.status is MetricStatus.skipped
    assert dict(result.detail) == {"reason": "reference_empty"}


def test_detail_reports_normalized_lengths_and_policy() -> None:
    result = prose_exact_match(" a  b ", "a b", _POLICY)
    assert result.detail["reference_length"] == 3
    assert result.detail["prediction_length"] == 3
    assert detail_map(result, "normalization") == _POLICY.as_detail()


# ── prose.token_prf: multiset counting, one result, long input ──

@pytest.mark.parametrize(
    ("metric", "extra_detail"),
    [(prose_token_prf, {"tp": 3, "fp": 0, "fn": 0}), (prose_rouge_l, {"lcs_length": 3})],
    ids=["token", "rouge"],
)
def test_full_overlap_scores_100_with_precision_and_recall_100(
    metric: Callable[[object, object, ProseNormalizationPolicy], ScoreResult],
    extra_detail: dict[str, int],
) -> None:
    result = metric("a b c", "a b c", _POLICY)
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert result.detail["precision"] == 100.0
    assert result.detail["recall"] == 100.0
    assert result.detail["f1"] == 100.0
    for key, value in extra_detail.items():
        assert result.detail[key] == value, key


def test_partial_overlap_reports_counts_and_totals() -> None:
    result = prose_token_prf("a b c", "a b d", _POLICY)
    assert result.detail["tp"] == 2
    assert result.detail["fp"] == 1
    assert result.detail["fn"] == 1
    assert result.detail["reference_tokens"] == 3
    assert result.detail["prediction_tokens"] == 3
    assert result.value == pytest.approx(2 * 2 / (2 * 2 + 1 + 1) * 100.0)


@pytest.mark.parametrize(
    ("reference", "prediction", "tp", "fp", "fn"),
    [("a a", "a", 1, 0, 1), ("a", "a a", 1, 1, 0)],
    ids=["missing_repeat_is_a_false_negative", "extra_repeat_is_a_false_positive"],
)
def test_multiset_counting_respects_repeats(
    reference: str, prediction: str, tp: int, fp: int, fn: int
) -> None:
    """Each repeat of a token is counted: one match uses up one occurrence on each side."""
    result = prose_token_prf(reference, prediction, _POLICY)
    assert (result.detail["tp"], result.detail["fp"], result.detail["fn"]) == (tp, fp, fn)


def test_value_duplicates_the_f1_detail_field() -> None:
    result = prose_token_prf("a b c d", "a b", _POLICY)
    assert result.value == result.detail["f1"]


def test_extremely_long_input_is_deterministic_and_correctly_counted() -> None:
    """Behavior must not change with input size, and nothing may be truncated."""
    reference = " ".join(f"token{index}" for index in range(50_000))
    prediction = " ".join(f"token{index}" for index in range(25_000))
    first = prose_token_prf(reference, prediction, _POLICY)
    second = prose_token_prf(reference, prediction, _POLICY)

    assert first.to_json() == second.to_json()
    assert first.detail["reference_tokens"] == 50_000
    assert first.detail["prediction_tokens"] == 25_000
    assert first.detail["tp"] == 25_000
    assert first.detail["fp"] == 0
    assert first.detail["fn"] == 25_000
    assert first.detail["precision"] == 100.0
    assert first.detail["recall"] == 50.0


def test_long_input_detail_carries_no_unbounded_field() -> None:
    """Counts scale with input size, but no per-token list is ever emitted."""
    text = " ".join(f"t{index}" for index in range(20_000))
    result = prose_token_prf(text, "t1 t2", _POLICY)
    assert set(result.detail) == {
        "precision",
        "recall",
        "f1",
        "tp",
        "fp",
        "fn",
        "reference_tokens",
        "prediction_tokens",
        "normalization",
    }
    assert len(result.to_json()) < 1000


# ── prose.rouge_l: LCS scoring and the token-product bound ──

def test_partial_overlap_reports_lcs_and_counts() -> None:
    result = prose_rouge_l("a b c d", "a x c", _POLICY)
    assert result.detail["lcs_length"] == 2
    assert result.detail["reference_tokens"] == 4
    assert result.detail["prediction_tokens"] == 3
    assert result.detail["precision"] == pytest.approx(2 / 3 * 100.0)
    assert result.detail["recall"] == pytest.approx(2 / 4 * 100.0)
    assert result.value == pytest.approx(2 * 2 / (3 + 4) * 100.0)


def test_reordered_tokens_score_below_token_prf() -> None:
    """The whole point of the metric: same multiset, different order."""
    reference, prediction = "a b c d", "d c b a"
    rouge = prose_rouge_l(reference, prediction, _POLICY)
    token = prose_token_prf(reference, prediction, _POLICY)
    assert token.value == 100.0
    assert rouge.detail["lcs_length"] == 1
    assert rouge.value is not None
    assert token.value is not None
    assert rouge.value < token.value


def test_repeated_tokens_are_matched_in_order_not_as_a_multiset() -> None:
    """``a a`` against ``a a a`` shares two tokens in order, not three."""
    result = prose_rouge_l("a a", "a a a", _POLICY)
    assert result.detail["lcs_length"] == 2
    assert result.detail["recall"] == 100.0
    assert result.detail["precision"] == pytest.approx(2 / 3 * 100.0)


def test_disjoint_sequences_score_zero() -> None:
    result = prose_rouge_l("a b c", "x y z", _POLICY)
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    assert result.detail["lcs_length"] == 0


def test_subsequence_need_not_be_contiguous() -> None:
    """LCS is a subsequence, not a substring: the gap in the middle does not break it."""
    result = prose_rouge_l("a b c", "a x b x c", _POLICY)
    assert result.detail["lcs_length"] == 3
    assert result.detail["recall"] == 100.0


def _at_product(reference_tokens: int, prediction_tokens: int) -> tuple[str, str]:
    return " ".join(["a"] * reference_tokens), " ".join(["a"] * prediction_tokens)


def test_exactly_at_the_bound_is_scored() -> None:
    """The bound is inclusive: 5,000,000 computes."""
    reference, prediction = _at_product(2_500_000, 2)
    result = prose_rouge_l(reference, prediction, _POLICY)
    assert result.status is MetricStatus.scored
    assert result.detail["reference_tokens"] == 2_500_000
    assert result.detail["prediction_tokens"] == 2


def test_one_token_over_the_bound_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Above the bound the LCS is never computed, so the skip costs nothing.

    ``_lcs_length`` is made to fail: checking the status alone would also pass on an
    implementation that computed the full table and discarded the answer.
    """

    def _explode(reference: tuple[str, ...], prediction: tuple[str, ...]) -> int:
        raise AssertionError("LCS must not run above the bound")

    monkeypatch.setattr(prose_metrics, "_lcs_length", _explode)
    reference, prediction = _at_product(2_500_001, 2)
    result = prose_rouge_l(reference, prediction, _POLICY)
    assert result.status is MetricStatus.skipped
    assert result.value is None
    assert result.error is None
    assert dict(result.detail) == {
        "reason": "rouge_l_input_too_large",
        "reference_tokens": 2_500_001,
        "prediction_tokens": 2,
        "token_product": 5_000_002,
        "max_token_product": 5_000_000,
    }


# ── ROUGE-L exclusions ──
# Each claim is negative, so each test names the permissive behavior a wrong implementation
# would exhibit and asserts it does not happen.

@pytest.mark.parametrize(
    ("reference", "prediction", "lcs_length", "reference_tokens"),
    [("running", "run", 0, 1), ("cats", "cat", 0, 1), ("the cat", "the cat", 2, 2)],
    ids=["no_stemming", "no_lemmatization", "stop_words_counted"],
)
def test_tokens_are_compared_as_written(
    reference: str, prediction: str, lcs_length: int, reference_tokens: int
) -> None:
    """No stemming, lemmatization or stop-word removal: every token counts as written."""
    result = prose_rouge_l(reference, prediction, _POLICY)
    assert result.detail["lcs_length"] == lcs_length
    assert result.detail["reference_tokens"] == reference_tokens


def test_multi_sentence_input_is_one_token_sequence_not_rouge_lsum() -> None:
    """No sentence splitting, so a single LCS spans the sentence boundary."""
    result = prose_rouge_l("a b. c d.", "a b. c d.", _POLICY)
    assert result.detail["lcs_length"] == 4
    assert result.value == 100.0


def test_sentence_order_matters_because_there_is_no_sentence_alignment() -> None:
    """ROUGE-Lsum would score these highly by aligning sentences; ROUGE-L does not."""
    result = prose_rouge_l("a b. c d.", "c d. a b.", _POLICY)
    assert result.detail["lcs_length"] == 2
    assert result.value != 100.0


# ── ROUGE-L against a naive LCS oracle ──
# An off-by-one in a hand-rolled dynamic program survives hand-picked examples, so the LCS
# length is compared with a separately written reference over an exhaustive corpus. The
# two-symbol alphabet maximizes repeated tokens, where LCS errors live.

_ALPHABET = ("a", "b")
_EXHAUSTIVE_MAX_LENGTH = 5
_GENERATED_MAX_LENGTH = 20


def _naive_lcs_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    """Full-table LCS, written independently of the production two-row version."""
    table = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i in range(1, len(left) + 1):
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                table[i][j] = table[i - 1][j - 1] + 1
            else:
                table[i][j] = max(table[i - 1][j], table[i][j - 1])
    return table[len(left)][len(right)]


def _sequences_up_to(max_length: int) -> Iterator[tuple[str, ...]]:
    for length in range(max_length + 1):
        yield from product(_ALPHABET, repeat=length)


_EXHAUSTIVE_PAIRS = [
    (left, right)
    for left in _sequences_up_to(_EXHAUSTIVE_MAX_LENGTH)
    for right in _sequences_up_to(_EXHAUSTIVE_MAX_LENGTH)
]


def test_exhaustive_agreement_with_the_naive_oracle() -> None:
    for left, right in _EXHAUSTIVE_PAIRS:
        assert _lcs_length(left, right) == _naive_lcs_length(left, right), (left, right)


@pytest.mark.parametrize("seed", range(50))
def test_generated_agreement_with_the_naive_oracle(seed: int) -> None:
    import random

    rng = random.Random(seed)  # noqa: S311 - a seeded PRNG for a deterministic oracle fixture, not security
    for _ in range(20):
        left = tuple(
            rng.choice(_ALPHABET) for _ in range(rng.randint(0, _GENERATED_MAX_LENGTH))
        )
        right = tuple(
            rng.choice(_ALPHABET) for _ in range(rng.randint(0, _GENERATED_MAX_LENGTH))
        )
        assert _lcs_length(left, right) == _naive_lcs_length(left, right), (left, right)


def test_metric_value_matches_the_oracle_derived_score() -> None:
    """The oracle check reaches the public value, not only the private helper."""
    reference, prediction = "a b a b a", "b a b a b"
    expected_lcs = _naive_lcs_length(tuple(reference.split()), tuple(prediction.split()))
    result = prose_rouge_l(reference, prediction, _POLICY)
    assert result.detail["lcs_length"] == expected_lcs
    assert result.value == pytest.approx(2 * expected_lcs / (5 + 5) * 100.0)


# ── phrase metrics: boundaries, deduplication, overlap, completeness ──

_CASEFOLD = ProseNormalizationPolicy(case=CaseMode.casefold)
_TEXT = "the threat actor used spearphishing for initial access"


def test_all_required_present_scores_100() -> None:
    result = prose_required_phrase_coverage(
        _TEXT, ("threat actor", "initial access"), _POLICY
    )
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert detail_list(result, "unmatched") == []


def test_no_forbidden_present_scores_100() -> None:
    result = prose_forbidden_phrase_absence(_TEXT, ("ransomware",), _POLICY)
    assert result.value == 100.0
    assert detail_list(result, "matched") == []


def test_partial_coverage_reports_matched_and_unmatched_in_caller_order() -> None:
    result = prose_required_phrase_coverage(
        _TEXT, ("threat actor", "kill chain", "initial access"), _POLICY
    )
    assert result.value == pytest.approx(2 / 3 * 100.0)
    assert detail_list(result, "matched") == ["threat actor", "initial access"]
    assert detail_list(result, "unmatched") == ["kill chain"]


def test_forbidden_absence_rewards_unmatched_and_lists_violations_as_matched() -> None:
    result = prose_forbidden_phrase_absence(
        _TEXT, ("spearphishing", "ransomware"), _POLICY
    )
    assert result.value == pytest.approx(1 / 2 * 100.0)
    assert detail_list(result, "matched") == ["spearphishing"]
    assert detail_list(result, "unmatched") == ["ransomware"]


def test_phrase_does_not_match_inside_a_larger_token() -> None:
    assert prose_required_phrase_coverage("concatenate now", ("cat",), _POLICY).value == 0.0


def test_phrase_token_order_matters() -> None:
    assert (
        prose_required_phrase_coverage("source open here", ("open source",), _POLICY).value
        == 0.0
    )
    assert (
        prose_required_phrase_coverage("open source here", ("open source",), _POLICY).value
        == 100.0
    )


def test_phrase_longer_than_the_prediction_is_absent() -> None:
    result = prose_required_phrase_coverage("short", ("a much longer phrase",), _POLICY)
    assert result.value == 0.0


def test_zero_token_prediction_with_a_configured_list_scores_zero() -> None:
    result = prose_required_phrase_coverage("   ", ("anything",), _POLICY)
    assert result.status is MetricStatus.scored
    assert result.value == 0.0


def test_duplicates_are_removed_after_normalization() -> None:
    result = prose_required_phrase_coverage(
        _TEXT, ("Threat Actor", "threat actor"), _CASEFOLD
    )
    assert result.detail["total"] == 1
    assert result.detail["duplicates_removed"] == 1
    assert result.value == 100.0


def test_unicode_composition_duplicates_are_removed() -> None:
    nfc = ProseNormalizationPolicy(unicode_form=UnicodeForm.NFC)
    result = prose_required_phrase_coverage(
        "caf\u00e9 open", ("caf\u00e9", "cafe\u0301"), nfc
    )
    assert result.detail["total"] == 1
    assert result.detail["duplicates_removed"] == 1


def test_overlapping_phrases_all_match_because_matching_consumes_nothing() -> None:
    result = prose_required_phrase_coverage(
        "the threat actor here", ("threat actor", "actor here", "the threat"), _POLICY
    )
    assert result.value == 100.0
    assert len(detail_list(result, "matched")) == 3


def test_diagnostics_are_complete_for_a_large_list() -> None:
    """Diagnostics list every phrase: no truncation, elision, sampling or cap."""
    phrases = tuple(f"phrase {index}" for index in range(500))
    result = prose_required_phrase_coverage("phrase 7 and phrase 42", phrases, _POLICY)
    matched = detail_list(result, "matched")
    unmatched = detail_list(result, "unmatched")
    assert result.detail["total"] == 500
    assert len(matched) + len(unmatched) == 500
    assert sorted(matched) == ["phrase 42", "phrase 7"]


_FORBIDDEN_WORDING = (
    "hallucinat",
    "fact verif",
    "claim verif",
    "semantic accuracy",
    "factual",
)


def test_no_result_field_claims_a_semantic_capability() -> None:
    results = (
        prose_required_phrase_coverage(_TEXT, ("threat actor",), _POLICY),
        prose_forbidden_phrase_absence(_TEXT, ("ransomware",), _POLICY),
    )
    for result in results:
        blob = result.to_json().lower() + result.metadata.label.lower()
        for banned in _FORBIDDEN_WORDING:
            assert banned not in blob


# ── ProseConfig: policy requirement, absent vs. empty lists, cross-list check ──

def test_policy_is_required_with_no_default() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        ProseConfig()  # type: ignore[call-arg]


def test_absent_and_empty_lists_are_distinct_configurations() -> None:
    absent = ProseConfig(policy=_POLICY)
    empty = ProseConfig(policy=_POLICY, required_phrases=(), forbidden_phrases=())
    assert absent.required_phrases is None
    assert empty.required_phrases == ()
    assert absent != empty


def test_config_is_frozen() -> None:
    config = ProseConfig(policy=_POLICY)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        config.required_phrases = ("x",)


def test_no_conflict_when_the_policy_keeps_the_phrases_distinct() -> None:
    config = ProseConfig(
        policy=_POLICY,
        required_phrases=("Threat Actor",),
        forbidden_phrases=("threat actor",),
    )
    assert config.required_phrases == ("Threat Actor",)


def test_direct_single_phrase_metric_performs_no_cross_list_check() -> None:
    """Calling a phrase metric directly bypasses the cross-list conflict check."""
    required = prose_required_phrase_coverage("the x here", ("x",), _CASEFOLD)
    forbidden = prose_forbidden_phrase_absence("the x here", ("x",), _CASEFOLD)
    assert required.status.value == "scored"
    assert forbidden.status.value == "scored"
    assert required.value == 100.0
    assert forbidden.value == 0.0


# ── exact, deterministic error content ──

_STRIP = ProseNormalizationPolicy(punctuation=PunctuationMode.strip)

# repr() of a caller object embeds a memory address, which would break golden fixtures.
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
_OBJECT_REPR = re.compile(r"<[a-zA-Z_.]+ object at ")

_REFERENCE_METRICS = [prose_exact_match, prose_token_prf, prose_rouge_l]
_REFERENCE_METRIC_IDS = ["exact", "token", "rouge"]

_PREDICTION_CALLS: list[tuple[str, Callable[[object], ScoreResult]]] = [
    ("exact", lambda p: prose_exact_match("reference", p, _POLICY)),
    ("token", lambda p: prose_token_prf("reference", p, _POLICY)),
    ("rouge", lambda p: prose_rouge_l("reference", p, _POLICY)),
    ("required", lambda p: prose_required_phrase_coverage(p, ("x",), _POLICY)),
    ("forbidden", lambda p: prose_forbidden_phrase_absence(p, ("x",), _POLICY)),
]


@pytest.mark.parametrize(
    ("prediction", "type_name"),
    [(b"x", "bytes"), (42, "int"), (None, "NoneType"), ([], "list"), ({}, "dict")],
    ids=["bytes", "int", "None", "list", "dict"],
)
@pytest.mark.parametrize(
    "call", [call for _, call in _PREDICTION_CALLS], ids=[name for name, _ in _PREDICTION_CALLS]
)
def test_invalid_message_names_the_type_not_the_value(
    call: Callable[[object], ScoreResult], prediction: object, type_name: str
) -> None:
    """A configured metric given a non-text prediction is ``invalid`` with one error code."""
    result = call(prediction)
    assert result.status is MetricStatus.invalid
    assert result.value == 0
    assert result.error is not None
    assert result.error.code == "prose_input_invalid"
    assert result.error.message == f"prose prediction must be str, got {type_name}"
    assert list(result.error.locations) == []
    assert dict(result.error.details) == {"received_type": type_name}


def test_error_fields_contain_no_address_identity_or_object_repr() -> None:
    class Opaque:
        pass

    result = prose_token_prf("ref", Opaque(), _POLICY)
    assert result.error is not None
    blob = result.to_json()
    assert not _ADDRESS.search(blob)
    assert not _OBJECT_REPR.search(blob)


# A list or tuple reference raises: multiple references are never scored against the
# first one, joined, or aggregated.
@pytest.mark.parametrize(
    ("reference", "type_name"),
    [(42, "int"), (["a b", "c d"], "list"), (("a b", "c d"), "tuple"), (object(), "object")],
    ids=["int", "list", "tuple", "object"],
)
@pytest.mark.parametrize("metric", _REFERENCE_METRICS, ids=_REFERENCE_METRIC_IDS)
def test_reference_configuration_error_message_is_exact(
    metric: Callable[[object, object, ProseNormalizationPolicy], ScoreResult],
    reference: object,
    type_name: str,
) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        metric(reference, "a b", _POLICY)
    assert str(excinfo.value) == f"prose reference must be str or None, got {type_name}"


_ZERO_TOKENS = "{list_name}[1] normalizes to zero tokens under the selected policy and can never match"


def _via_config(list_name: str, phrases: object, policy: ProseNormalizationPolicy) -> None:
    ProseConfig(policy=policy, **{list_name: phrases})  # type: ignore[arg-type]


def _via_metric(list_name: str, phrases: object, policy: ProseNormalizationPolicy) -> None:
    metric = {
        "required_phrases": prose_required_phrase_coverage,
        "forbidden_phrases": prose_forbidden_phrase_absence,
    }[list_name]
    metric("threat actor was here", phrases, policy)  # type: ignore[arg-type]


@pytest.mark.parametrize("list_name", ["required_phrases", "forbidden_phrases"])
@pytest.mark.parametrize("entry", [_via_config, _via_metric], ids=["config", "metric"])
@pytest.mark.parametrize(
    ("phrases", "policy", "message"),
    [
        # A str is itself a Sequence[str]: iterating it would score each character.
        (
            "threat actor",
            _POLICY,
            "{list_name} must be a sequence of phrases, not a single str; "
            "pass ('threat actor',) to supply one phrase",
        ),
        (("ok", 7), _POLICY, "{list_name}[1] must be str, got int"),
        (("ok", ""), _POLICY, _ZERO_TOKENS),
        (("ok", "   "), _POLICY, _ZERO_TOKENS),
        (("ok", "!!!"), _STRIP, _ZERO_TOKENS),
    ],
    ids=["bare_string", "non_text_element", "empty", "whitespace_only", "punctuation_under_strip"],
)
def test_phrase_configuration_error_message_is_exact(
    phrases: object,
    policy: ProseNormalizationPolicy,
    message: str,
    entry: Callable[[str, object, ProseNormalizationPolicy], None],
    list_name: str,
) -> None:
    """Both entry paths reject the same phrase lists with the same list name and index."""
    with pytest.raises(ConfigurationError) as excinfo:
        entry(list_name, phrases, policy)
    assert str(excinfo.value) == message.format(list_name=list_name)


def test_phrase_validity_is_policy_dependent() -> None:
    """A phrase is validated after normalization: ``!!!`` is valid when punctuation is kept."""
    config = ProseConfig(policy=_POLICY, required_phrases=("!!!",))
    assert config.required_phrases == ("!!!",)
    assert prose_required_phrase_coverage("a !!! b", ("!!!",), _POLICY).value == 100.0


@pytest.mark.parametrize(
    ("policy", "required", "forbidden", "conflicts"),
    [
        (_POLICY, ("beta", "alpha"), ("alpha", "beta"), "['alpha', 'beta']"),
        # The conflict is created by the policy and reported once after deduplication.
        (_CASEFOLD, ("Threat Actor", "Threat Actor"), ("threat actor",), "['threat actor']"),
    ],
    ids=["sorted", "normalized_and_deduplicated"],
)
def test_conflict_error_message_is_exact_and_sorted(
    policy: ProseNormalizationPolicy,
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
    conflicts: str,
) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        ProseConfig(policy=policy, required_phrases=required, forbidden_phrases=forbidden)
    assert str(excinfo.value) == (
        f"phrase appears in both required_phrases and forbidden_phrases after normalization: {conflicts}"
    )


# ── precedence order ──
# Steps: (1) configuration validity raises, (2) unconfigured metrics skip, (3) an unusable
# prediction is invalid, (4) everything else scores.

_Metric = Callable[[object, object, ProseNormalizationPolicy], ScoreResult]


def test_step_1_beats_step_3_for_phrase_validity() -> None:
    """An invalid phrase raises even when the prediction is also unusable."""
    with pytest.raises(ConfigurationError):
        prose_required_phrase_coverage(b"bytes", ("",), _POLICY)


_UNCONFIGURED: list[tuple[str, Callable[[object], ScoreResult], str]] = [
    ("exact", lambda p: prose_exact_match(None, p, _POLICY), "reference_not_configured"),
    ("token", lambda p: prose_token_prf(None, p, _POLICY), "reference_not_configured"),
    ("rouge", lambda p: prose_rouge_l(None, p, _POLICY), "reference_not_configured"),
    (
        "required_absent",
        lambda p: prose_required_phrase_coverage(p, None, _POLICY),
        "required_phrases_not_configured",
    ),
    (
        "required_empty",
        lambda p: prose_required_phrase_coverage(p, (), _POLICY),
        "required_phrases_empty",
    ),
    (
        "forbidden_absent",
        lambda p: prose_forbidden_phrase_absence(p, None, _POLICY),
        "forbidden_phrases_not_configured",
    ),
    (
        "forbidden_empty",
        lambda p: prose_forbidden_phrase_absence(p, (), _POLICY),
        "forbidden_phrases_empty",
    ),
]


@pytest.mark.parametrize("prediction", ["text", b"bytes"], ids=["text", "bytes"])
@pytest.mark.parametrize(
    ("call", "reason"),
    [(call, reason) for _, call, reason in _UNCONFIGURED],
    ids=[name for name, _, _ in _UNCONFIGURED],
)
def test_step_2_configuration_presence_yields_skipped(
    call: Callable[[object], ScoreResult], reason: str, prediction: object
) -> None:
    """An unconfigured metric reports ``skipped``, never ``invalid``, whatever the prediction."""
    result = call(prediction)
    assert result.status is MetricStatus.skipped
    assert result.value is None
    assert result.error is None
    assert dict(result.detail) == {"reason": reason}


@pytest.mark.parametrize("metric", [prose_token_prf, prose_rouge_l], ids=["token", "rouge"])
@pytest.mark.parametrize(
    ("reference", "prediction", "policy"),
    [("!!!", "???", _STRIP)],
    ids=["punctuation_under_strip"],
)
def test_step_3b_applies_to_both_token_sequence_metrics(
    metric: _Metric, reference: str, prediction: str, policy: ProseNormalizationPolicy
) -> None:
    """Zero tokens is decided after normalization, and before the ROUGE-L bound."""
    result = metric(reference, prediction, policy)
    assert result.status is MetricStatus.skipped
    assert result.value is None
    assert dict(result.detail) == {"reason": "no_tokens_on_either_side"}


@pytest.mark.parametrize(
    ("metric", "reference", "prediction", "expected_detail"),
    [
        (prose_token_prf, "a b", "", {"precision": 0.0, "recall": 0.0, "prediction_tokens": 0}),
        (prose_rouge_l, "a b", "", {"lcs_length": 0, "prediction_tokens": 0}),
    ],
    ids=["token/empty_prediction", "rouge/empty_prediction"],
)
def test_one_side_with_zero_tokens_scores_zero(
    metric: _Metric, reference: str, prediction: str, expected_detail: dict[str, object]
) -> None:
    result = metric(reference, prediction, _POLICY)
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    for key, value in expected_detail.items():
        assert result.detail[key] == value, key


def test_step_3c_applies_to_rouge_l_only() -> None:
    """The same oversized input scores on the multiset metric and skips on ROUGE-L."""
    oversized = " ".join(["a"] * 2_500_001)
    assert prose_token_prf(oversized, "a a", _POLICY).status is MetricStatus.scored
    bounded = prose_rouge_l(oversized, "a a", _POLICY)
    assert bounded.status is MetricStatus.skipped
    assert bounded.detail["reason"] == "rouge_l_input_too_large"


# ── routing: fixed key order, unconfigured sentinel, error propagation ──

_REFERENCE = "The threat actor used spearphishing for initial access."
_PREDICTION = "the threat actor used spearphishing"
_EXPECTED_ORDER = [
    "prose.exact_match",
    "prose.token_prf",
    "prose.rouge_l",
    "prose.required_phrase_coverage",
    "prose.forbidden_phrase_absence",
]


def _config(**kwargs: object) -> FamilyConfig:
    return FamilyConfig(prose=ProseConfig(policy=_CASEFOLD, **kwargs))  # type: ignore[arg-type]


def test_unconfigured_routing_is_a_configuration_error() -> None:
    """Without a ``ProseConfig`` there is no normalization policy: the caller's mistake,
    never a score."""
    with pytest.raises(ConfigurationError, match="prose"):
        run_for_family("the actor", "the actor", Family.PROSE, FamilyConfig())


def test_key_order_does_not_vary_with_configuration_or_outcome() -> None:
    """Key order is fixed, so serializing the whole mapping is byte-stable."""
    minimal = run_for_family(None, "text", Family.PROSE, _config())
    full = run_for_family(
        _REFERENCE,
        _PREDICTION,
        Family.PROSE,
        _config(required_phrases=("threat actor",), forbidden_phrases=("ransomware",)),
    )
    malformed = run_for_family(_REFERENCE, b"bytes", Family.PROSE, _config())
    assert list(minimal) == list(full) == list(malformed) == _EXPECTED_ORDER


def test_all_five_keys_present_even_when_metrics_are_skipped() -> None:
    results = run_for_family(None, "text", Family.PROSE, _config())
    assert list(results) == _EXPECTED_ORDER
    assert all(result.status is MetricStatus.skipped for result in results.values())


def test_configuration_error_from_a_metric_propagates_through_routing() -> None:
    with pytest.raises(ConfigurationError):
        run_for_family(123, "a", Family.PROSE, _config())


# ── golden fixtures ──
# `prose_results.json` pins each case's `ScoreResult.to_json()` as exact bytes, keyed by
# case name (`_regenerate`).

_RESULTS = FIXTURES_DIR / "prose_results.json"

CASES: dict[str, dict[str, Any]] = {
    # Every metric at 100.
    "pass_full_match": {
        "reference": "The threat actor used spearphishing.",
        "prediction": "The threat actor used spearphishing.",
        "policy": {},
        "required_phrases": ["threat actor"],
        "forbidden_phrases": ["ransomware"],
    },
    # Partial token overlap and a forbidden-phrase violation.
    "partial_overlap": {
        "reference": "the threat actor used spearphishing for initial access",
        "prediction": "the actor used phishing for access",
        "policy": {},
        "required_phrases": ["threat actor", "initial access"],
        "forbidden_phrases": ["ransomware", "phishing"],
    },
    # Phrases only, under the casefold policy, with a duplicate removed.
    "phrase_pass_and_violation": {
        "reference": None,
        "prediction": "The Threat Actor deployed ransomware payloads.",
        "policy": {"case": "casefold"},
        "required_phrases": ["threat actor", "kill chain", "Threat Actor"],
        "forbidden_phrases": ["ransomware", "wiper"],
    },
    # Composition differences, an emoji and non-ASCII text in the diagnostics. Not CJK
    # tokenisation: the CJK run is space-delimited and identical on both sides, so it
    # normalises a CJK code point and cannot show the single-token collapse of an
    # unspaced script.
    "unicode_forms_and_scripts": {
        "reference": "caf\u00e9 na\u00efve \u653b\u6483\u8005 \U0001f512",
        "prediction": "cafe\u0301 na\u00efve \u653b\u6483\u8005 \U0001f512",
        "policy": {"unicode_form": "NFC"},
        "required_phrases": ["caf\u00e9", "\U0001f512"],
        "forbidden_phrases": ["\u0432\u0438\u0440\u0443\u0441"],
    },
    # `NFKC` folding of ligatures, superscripts and full-width forms.
    "compatibility_folding": {
        "reference": "\ufb01le x\u00b2 \uff21\uff22",
        "prediction": "file x2 AB",
        "policy": {"unicode_form": "NFKC"},
        "required_phrases": ["file"],
        "forbidden_phrases": ["\ufb02ag"],
    },
    # `punctuation="strip"` splitting `snake_case` and `state-of-the-art`.
    "connector_punctuation": {
        "reference": "snake_case state-of-the-art",
        "prediction": "snake case state of the art",
        "policy": {"punctuation": "strip"},
        "required_phrases": ["snake case"],
        "forbidden_phrases": ["camelCase"],
    },
    # Markup scored as raw text, never parsed.
    "markdown_raw_text": {
        "reference": "see **the report** at <b>host</b>",
        "prediction": "see the report at host",
        "policy": {},
        "required_phrases": ["**the report**"],
        "forbidden_phrases": ["&amp;"],
    },
    # `prose.rouge_l` above its token-product bound: `skipped` with
    # `rouge_l_input_too_large` and its five-field diagnostic. The recorded result carries
    # `max_token_product`, so changing the bound fails this case on purpose: the bound is a
    # contract, changed only deliberately.
    "rouge_l_bounded": {
        "reference_repeat_tokens": 2_500_001,
        "prediction": "a a",
        "policy": {},
        "required_phrases": None,
        "forbidden_phrases": None,
    },
    # A prediction that is not text, and skipped winning over invalid.
    "malformed_prediction": {
        "reference": "the threat actor",
        "prediction_bytes": True,
        "policy": {},
        "required_phrases": ["threat actor"],
        "forbidden_phrases": None,
    },
}


def _prediction(case: dict[str, Any]) -> object:
    if case.get("prediction_bytes"):
        return b"\x00binary"
    return case["prediction"]


def _policy(case: dict[str, Any]) -> ProseNormalizationPolicy:
    return ProseNormalizationPolicy(**case["policy"])


def _reference(case: dict[str, Any]) -> object:
    """Resolve the reference, expanding the repeat descriptor if present.

    The bounded case needs millions of tokens, so its case holds a descriptor.
    """
    repeat = case.get("reference_repeat_tokens")
    if repeat is not None:
        return " ".join(["a"] * int(repeat))
    return case.get("reference")


def _phrases(case: dict[str, Any], key: str) -> tuple[str, ...] | None:
    raw = case.get(key)
    return None if raw is None else tuple(raw)


def _direct(case: dict[str, Any]) -> dict[str, str]:
    policy = _policy(case)
    reference = _reference(case)
    prediction = _prediction(case)
    return {
        "prose.exact_match": prose_exact_match(reference, prediction, policy).to_json(),
        "prose.token_prf": prose_token_prf(reference, prediction, policy).to_json(),
        "prose.rouge_l": prose_rouge_l(reference, prediction, policy).to_json(),
        "prose.required_phrase_coverage": prose_required_phrase_coverage(
            prediction, _phrases(case, "required_phrases"), policy
        ).to_json(),
        "prose.forbidden_phrase_absence": prose_forbidden_phrase_absence(
            prediction, _phrases(case, "forbidden_phrases"), policy
        ).to_json(),
    }


def _routed(case: dict[str, Any]) -> dict[str, str]:
    config = FamilyConfig(
        prose=ProseConfig(
            policy=_policy(case),
            required_phrases=_phrases(case, "required_phrases"),
            forbidden_phrases=_phrases(case, "forbidden_phrases"),
        )
    )
    results = run_for_family(
        _reference(case), _prediction(case), Family.PROSE, config
    )
    return {key: results[key].to_json() for key in results}


@functools.cache
def _recorded() -> dict[str, dict[str, str]]:
    return json.loads(_RESULTS.read_text(encoding="utf-8"))


def test_the_recorded_results_cover_exactly_the_cases() -> None:
    assert set(_recorded()) == set(CASES), "run _regenerate() after changing CASES"


@pytest.mark.parametrize("name", sorted(CASES))
def test_serialized_results_match_the_recorded_bytes(name: str) -> None:
    assert _direct(CASES[name]) == _recorded()[name]


@pytest.mark.parametrize("name", sorted(CASES))
def test_routed_output_equals_direct_output(name: str) -> None:
    assert _routed(CASES[name]) == _direct(CASES[name])


def _regenerate() -> None:
    """Rewrite `prose_results.json` from the current code.

    Only for a change to the output that is intended. The bytes depend on the interpreter:
    the Unicode forms and the punctuation classes come from its `unicodedata` tables, which
    a Python upgrade can change. A comparison that fails after an interpreter or dependency
    upgrade is a break of the serialization contract, never a reason to regenerate: it is a
    new version of the package, its `__version__` bumped and the change noted in
    `app/evaluation_metrics/__init__.py`. To regenerate::

        cd backend && python -c "from tests.test_metric_suite_prose import _regenerate; _regenerate()"
    """
    results = {name: _direct(case) for name, case in CASES.items()}
    _RESULTS.write_text(
        json.dumps(results, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
