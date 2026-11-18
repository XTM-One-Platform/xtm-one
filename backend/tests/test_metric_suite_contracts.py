"""The metric suite's contracts: the public surfaces of `core`, `quality` and
`performance`, the import boundaries between them, the `ScoreResult` and `PerformanceResult`
contracts and their serialization, performance metric identity, the rollup declaration, and
the no-I/O rule of the performance package.
"""

from __future__ import annotations

import ast
import math
import pathlib
import re
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import BaseModel, ValidationError

import app.evaluation_metrics.core as core
import app.evaluation_metrics.performance as performance
import app.evaluation_metrics.performance as performance_pkg
import app.evaluation_metrics.quality as quality
from app.evaluation_metrics.core.contracts import (
    MACRO_CLASSES,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    MetricDirection,
    MetricError,
    MetricRollup,
    MetricStatus,
    check_rollup_counts,
)
from app.evaluation_metrics.performance import metrics as performance_metrics
from app.evaluation_metrics.performance.metrics import METRIC_METADATA, reliability
from app.evaluation_metrics.performance.results import PerformanceMetadata, PerformanceResult
from app.evaluation_metrics.performance.telemetry import (
    CallKind,
    CallOutcome,
    CallRecord,
    CallScope,
    ExecutionTelemetry,
)
from app.evaluation_metrics.performance.units import MetricUnit
from app.evaluation_metrics.quality.results import MetricMetadata, ScoreResult
from app.services.evaluation.metric_package import registry
from app.services.evaluation.metric_package.registry import build_registry
from tests.evaluation_helpers import PACKAGE_DIR

# ── core public surface ──
# The immutable container types and their serialization helpers live in `core.serialization`
# but stay out of `__all__`, so the versioned surface does not freeze serialization mechanics.

PUBLIC = {
    "ConfigurationError",
    "EvaluationMetricsError",
    "InternalContractError",
    "JSONValue",
    "MetricDirection",
    "MetricError",
    "MetricStatus",
}


def test_core_exports_exactly_the_shared_contracts() -> None:
    assert set(core.__all__) == PUBLIC


@pytest.mark.parametrize("namespace", [core, quality], ids=["core", "quality"])
def test_every_declared_export_actually_resolves(namespace: ModuleType) -> None:
    for name in namespace.__all__:
        assert hasattr(namespace, name), f"{name} is declared in __all__ but missing"


# ── public surfaces ──
# Compared as sets, never a count: a count passes on a rename and never names the
# missing symbol. The export surface is a versioned contract: a symbol leaves it only at a
# major bump, joins `_REMOVED_SYMBOLS`, and stays unreachable.

_PERFORMANCE_SURFACE = {
    "CallKind",
    "CallOutcome",
    "CallRecord",
    "CallScope",
    "CostBasis",
    "ExecutionTelemetry",
    "MetricUnit",
    "ModelTokenUsage",
    "NormalizedTokenUsage",
    "PerformanceMetadata",
    "PerformanceResult",
    "PriceTable",
    "TelemetryValidation",
    "TokenConvention",
    "TokenUsage",
    "call_counts",
    "estimated_llm_cost",
    "normalize_token_usage",
    "reliability",
    "retry_count",
    "time_to_first_token",
    "token_usage_total",
    "validate_telemetry",
    "wall_clock_latency",
}

_QUALITY_SURFACE = {
    "ArraySemantics",
    "Canonicalizer",
    "CaseMode",
    "ComparisonPolicy",
    "DiagnosticSet",
    "Family",
    "FamilyConfig",
    "MetricMetadata",
    "ProseConfig",
    "ProseNormalizationPolicy",
    "PunctuationMode",
    "ScoreResult",
    "StixObjectTypeSelection",
    "StixValidationReport",
    "StixValidator",
    "UnicodeForm",
    "WhitespaceMode",
    "classification_dataset",
    "json_field_accuracy",
    "json_schema_validity",
    "label_vocabulary_validity",
    "macro_average",
    "multiset_diagnostics",
    "precision_recall_f1",
    "prose_exact_match",
    "prose_forbidden_phrase_absence",
    "prose_required_phrase_coverage",
    "prose_rouge_l",
    "prose_token_prf",
    "run_for_family",
    "set_diagnostics",
    "stix_entity_scores",
    "stix_envelope_validity",
    "stix_relationship_scores",
    "stix_semantic_validity",
    "trim_edge_marks",
    "unreadable_answer",
}

# The `core` symbols, never re-exported by `quality`.
_CORE_SYMBOLS = {
    "ConfigurationError",
    "EvaluationMetricsError",
    "JSONValue",
    "MetricDirection",
    "MetricError",
    "MetricStatus",
}

_REMOVED_SYMBOLS = {
    "MacroUniverse",
    "Metric",
    "PerformanceConfig",
    "TechniqueMatchMode",
    "attack_technique_scores",
    "classification_example",
    "json_syntactic_validity",
    "micro_average",
    "per_label_scores",
    "positional_diagnostics",
}


def test_performance_exports_match_the_published_surface() -> None:
    assert set(performance.__all__) == _PERFORMANCE_SURFACE
    assert not any(hasattr(performance, name) for name in _REMOVED_SYMBOLS)


def test_quality_exports_match_the_published_surface() -> None:
    assert set(quality.__all__) == _QUALITY_SURFACE
    assert _QUALITY_SURFACE.isdisjoint(_CORE_SYMBOLS)
    assert not any(hasattr(quality, name) for name in _REMOVED_SYMBOLS)


def test_exports_are_sorted() -> None:
    assert list(quality.__all__) == sorted(quality.__all__)


# The status set and the published metric keys are versioned contracts.
_EXPECTED_STATUSES = {"scored", "invalid", "skipped", "unavailable"}

_PUBLISHED_METRIC_KEYS = {
    "classification.macro_prf",
    "classification.micro_prf",
    "classification.samples_f1",
    "classification.subset_accuracy",
    "classification.vocabulary_validity",
    "json.field_accuracy",
    "json.schema_validity",
    "prose.exact_match",
    "prose.rouge_l",
    "prose.token_prf",
    "stix.entity_prf",
    "stix.entity_prf.aligned",
    "stix.entity_prf.aligned.closed",
    "stix.entity_prf.by_type",
    "stix.entity_prf.soft",
    "stix.envelope_validity",
    "stix.relationship_prf",
    "stix.relationship_prf.aligned",
    "stix.relationship_prf.aligned.closed",
    "stix.semantic_validity",
}


def test_metric_status_set_is_unchanged() -> None:
    """No new metric status may be introduced: the status set is a versioned contract."""
    assert {member.value for member in MetricStatus} == _EXPECTED_STATUSES


# Withdrawn at a major bump rather than redefined, so no key changes meaning.
_WITHDRAWN_METRIC_KEYS = {
    "attack.techniques.strict",
    "attack.techniques.parent_rollup",
    "classification.example.set_prf",
    "classification.hamming_loss",
    "family.unavailable",
    "json.syntactic_validity",
}


def test_declared_quality_keys_are_exactly_the_published_ones_and_withdrawn_ones_never_return() -> None:
    """The declared quality keys are exactly the published ones, and no withdrawn one is.

    Stored scores and run headlines are keyed by them, so a renamed, removed or added key
    fails here. A withdrawn key is never redefined, so no stored score changes
    meaning.
    """
    declared = {
        d.key
        for d in build_registry(quality, performance_metrics).definitions
        if d.half == "quality"
    }
    assert declared == _PUBLISHED_METRIC_KEYS, (
        "a missing or renamed key breaks the stored scores keyed by it: restore it; "
        "a new key is published on purpose: add it to _PUBLISHED_METRIC_KEYS"
    )
    assert declared.isdisjoint(_WITHDRAWN_METRIC_KEYS)


def test_prose_exports_no_aggregation_symbol() -> None:
    """The prose family exports no aggregation, weighting or averaging helper.

    What a set of scores means together is a policy the caller owns.
    """
    from app.evaluation_metrics.quality.families import prose

    offenders = [
        name
        for name in prose.__all__
        if re.search(r"aggregat|weighted?|macro_average|micro_average", name, re.IGNORECASE)
    ]
    assert offenders == [], f"prose exports aggregation surface: {offenders}"


# ── namespace import boundaries ──
# `core`, `quality` and `performance` stay independent of one another, and no module of the
# suite reaches back into the surrounding platform.

ROOT = PACKAGE_DIR
SUITE = "app.evaluation_metrics"
PLATFORM_ROOT = "app"


def _module_name(path: Path) -> str:
    parts = [p for p in path.relative_to(ROOT).with_suffix("").parts if p != "__init__"]
    return ".".join([*SUITE.split("."), *parts])


def _imports(path: Path) -> set[str]:
    """Every module `path` imports, with relative imports resolved to dotted paths.

    `from ...models import x` reaches `app.models` as surely as the absolute form, so
    skipping relative imports would open every boundary checked below.
    """
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    out.add(node.module)
                continue
            base = package.split(".")
            anchor = base[: len(base) - node.level + 1]
            if not anchor:
                continue
            if node.module:
                out.add(".".join([*anchor, node.module]))
                continue
            # `from .. import sibling` carries the module name in the alias list, so
            # recording the anchor alone would hide the sibling from every check below.
            out.add(".".join(anchor))
            out.update(
                ".".join([*anchor, alias.name]) for alias in node.names if alias.name != "*"
            )
    return out


def _namespace_files(namespace: str) -> list[Path]:
    """Every module in a namespace.

    ``rglob`` on a missing directory yields nothing, so a renamed namespace would make
    every boundary test pass over zero files; the assertion turns that into a failure.
    """
    files = sorted((ROOT / namespace).rglob("*.py"))
    assert files, (
        f"no modules found under {ROOT / namespace}: the namespace has been renamed or "
        "moved, and the import-boundary tests are scanning nothing"
    )
    return files


# (namespace, scan only its `__init__`, namespaces it must not import). `None` forbids
# every suite namespace but its own: an `__init__` re-exporting `core` would widen a
# versioned surface, while ordinary modules import `core` legitimately.
_BOUNDARIES = [
    pytest.param("core", False, ("quality", "performance"), id="core-imports-no-sibling"),
    pytest.param("quality", False, ("performance",), id="quality-never-imports-performance"),
    pytest.param("performance", False, ("quality",), id="performance-never-imports-quality"),
    pytest.param("core", True, None, id="core-init-imports-only-core"),
    pytest.param("quality", True, None, id="quality-init-imports-only-quality"),
    pytest.param("performance", True, None, id="performance-init-imports-only-performance"),
]


@pytest.mark.parametrize(("namespace", "init_only", "forbidden"), _BOUNDARIES)
def test_namespace_import_boundaries(
    namespace: str, init_only: bool, forbidden: tuple[str, ...] | None
) -> None:
    own = f"{SUITE}.{namespace}"
    files = [ROOT / namespace / "__init__.py"] if init_only else _namespace_files(namespace)

    def crosses(imported: str) -> bool:
        if forbidden is None:
            return imported.startswith(SUITE) and not imported.startswith(own)
        return any(imported.startswith(f"{SUITE}.{other}") for other in forbidden)

    offenders = sorted(
        f"{path.relative_to(ROOT)}: {imported}"
        for path in files
        for imported in _imports(path)
        if crosses(imported)
    )
    assert offenders == [], f"{namespace} crosses its import boundary: {offenders}"


def test_no_suite_module_imports_platform_code() -> None:
    """No suite module imports platform code under `app`, so the suite stays liftable.

    The predicate blacklists `app` outside the suite rather than whitelisting
    dependencies: the suite legitimately uses third-party packages.
    """
    files = sorted(ROOT.rglob("*.py"))
    assert files, (
        f"no modules found under {ROOT}: the metric suite has been renamed or moved, "
        "and this guard is scanning nothing"
    )
    offenders: list[str] = []
    for path in files:
        for imported in _imports(path):
            if imported != PLATFORM_ROOT and not imported.startswith(f"{PLATFORM_ROOT}."):
                continue
            if imported == SUITE or imported.startswith(f"{SUITE}."):
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {imported}")
    offenders.sort()
    assert offenders == [], (
        "the metric suite must not import platform code; reach the platform through "
        f"app/services/evaluation/metric_package/ instead: {offenders}"
    )


def test_prose_modules_import_only_stdlib_pydantic_and_in_package() -> None:
    """The prose family stays domain-agnostic: it may import only stdlib, pydantic and
    in-package modules."""
    allowed_prefixes = ("app.evaluation_metrics", "pydantic")
    stdlib = {"unicodedata", "enum", "typing", "dataclasses", "collections", "collections.abc"}
    offenders: list[str] = []
    paths = [
        *sorted((ROOT / "quality" / "families" / "prose").rglob("*.py")),
        ROOT / "quality" / "normalization" / "prose.py",
    ]
    assert len(paths) > 1, f"the prose family has no modules under {ROOT}: the scan is empty"
    for path in paths:
        for imported in _imports(path):
            if imported.startswith(allowed_prefixes) or imported in stdlib:
                continue
            offenders.append(f"{path.name}: {imported}")
    assert offenders == [], f"prose modules import outside stdlib/pydantic/package: {offenders}"


# ── ScoreResult contract and serialization ──
# Per-status value rules, deep immutability of `detail`/`error`, and byte-identical
# sorted-key compact JSON that rejects NaN and preserves non-ASCII.

META = MetricMetadata(
    key="test.metric",
    label="Test",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better, rollup="mean",
)


def _scored(value: float = 42.5) -> ScoreResult:
    return ScoreResult(status=MetricStatus.scored, value=value, metadata=META)


_ERR = MetricError(code="json_parse_error", message="bad")


@pytest.mark.parametrize(
    ("status", "value", "error", "accepted"),
    [
        pytest.param(MetricStatus.scored, 42.5, None, True, id="scored-in-range"),
        pytest.param(MetricStatus.scored, 100.0, None, True, id="scored-at-the-top"),
        pytest.param(MetricStatus.scored, 101.0, None, False, id="scored-above-100"),
        pytest.param(MetricStatus.scored, -1.0, None, False, id="scored-below-0"),
        pytest.param(MetricStatus.scored, None, None, False, id="scored-without-value"),
        pytest.param(MetricStatus.scored, 42.5, _ERR, False, id="scored-with-error"),
        pytest.param(MetricStatus.invalid, 0, _ERR, True, id="invalid-zero-with-error"),
        pytest.param(MetricStatus.invalid, 0, None, False, id="invalid-without-error"),
        pytest.param(MetricStatus.invalid, 1.0, _ERR, False, id="invalid-nonzero"),
        pytest.param(MetricStatus.skipped, None, None, True, id="skipped-without-value"),
        pytest.param(MetricStatus.skipped, 1.0, None, False, id="skipped-with-value"),
        pytest.param(MetricStatus.skipped, None, _ERR, False, id="skipped-with-error"),
        pytest.param(MetricStatus.unavailable, None, None, True, id="unavailable-without-value"),
        pytest.param(MetricStatus.unavailable, 0.0, None, False, id="unavailable-with-value"),
    ],
)
def test_status_value_and_error_invariants(
    status: MetricStatus, value: float | None, error: MetricError | None, accepted: bool
) -> None:
    """Each status admits only its own value and error shape, so no result is ambiguous."""
    if not accepted:
        with pytest.raises(ValidationError):
            ScoreResult(status=status, value=value, error=error, metadata=META)
        return
    r = ScoreResult(status=status, value=value, error=error, metadata=META)
    assert r.status is status
    assert r.value == value
    assert r.error == error


def test_metadata_empty_key_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricMetadata(
            key="",
            label="x",
            kind="prf",
            primary_measure="f1",
            direction=MetricDirection.higher_is_better, rollup="mean",
        )


def test_to_json_is_canonical_and_byte_identical() -> None:
    r = _scored()
    a = r.to_json()
    b = r.to_json()
    assert a == b
    assert a.encode("utf-8") == b.encode("utf-8")
    # sorted keys => "metadata" before "status" before "value"
    assert a.index('"metadata"') < a.index('"status"') < a.index('"value"')
    # compact separators, no spaces
    assert ", " not in a
    assert '": ' not in a


def test_to_json_preserves_non_ascii() -> None:
    err = MetricError(code="c", message="caf\u00e9 \u2013 na\u00efve")
    r = ScoreResult(status=MetricStatus.invalid, value=0, error=err, metadata=META)
    assert "caf\u00e9" in r.to_json()


def test_to_json_rejects_nan() -> None:
    r = ScoreResult.model_construct(
        status=MetricStatus.scored, value=math.nan, detail={}, error=None, metadata=META
    )
    with pytest.raises(ValueError):
        r.to_json()


def test_locations_are_immutable() -> None:
    err = MetricError(code="c", message="m", locations=["/a", "/b"])
    with pytest.raises(TypeError):
        err.locations[0] = "/z"  # type: ignore[index]


def test_details_are_deeply_immutable() -> None:
    err = MetricError(code="c", message="m", details={"nested": {"k": [1, 2]}})
    with pytest.raises(TypeError):
        err.details["new"] = 1  # type: ignore[index]
    nested = err.details["nested"]
    assert isinstance(nested, Mapping)
    with pytest.raises(TypeError):
        nested["k"] = 9  # type: ignore[index]


def test_detail_is_deeply_immutable() -> None:
    r = ScoreResult(
        status=MetricStatus.scored,
        value=1.0,
        detail={"outer": {"inner": 1}},
        metadata=META,
    )
    with pytest.raises(TypeError):
        r.detail["x"] = 1  # type: ignore[index]
    inner = r.detail["outer"]
    assert isinstance(inner, Mapping)
    with pytest.raises(TypeError):
        inner["inner"] = 2  # type: ignore[index]


def test_immutability_preserves_canonical_serialization() -> None:
    detail = {"b": [3, 2, 1], "a": {"n": 1}}
    r = ScoreResult(
        status=MetricStatus.scored, value=1.0, detail=dict(detail), metadata=META
    )
    # Frozen representation still round-trips to the documented JSON shape.
    assert r.to_dict()["detail"] == {"b": [3, 2, 1], "a": {"n": 1}}
    assert r.to_json() == r.to_json()


# ── PerformanceResult contract ──
# Unlike `ScoreResult`, its value is unbounded and unit-qualified, and only `invalid`
# carries an error.

_META = PerformanceMetadata(
    key="performance.latency.wall_clock",
    label="End-to-End Latency",
    kind="duration",
    primary_measure="wall_clock",
    unit=MetricUnit.milliseconds,
    direction=MetricDirection.lower_is_better, rollup="mean",
)


def _error() -> MetricError:
    return MetricError(code="malformed_telemetry", message="negative duration")


_SCORED, _INVALID, _SKIPPED, _UNAVAILABLE = (
    MetricStatus.scored,
    MetricStatus.invalid,
    MetricStatus.skipped,
    MetricStatus.unavailable,
)
_REASON = {"reason": "wall_clock_not_captured"}


@pytest.mark.parametrize(
    ("status", "value", "with_error", "detail"),
    [
        (_SCORED, None, False, {}),
        (_SCORED, 1.0, True, {}),
        (_SCORED, math.nan, False, {}),
        (_SCORED, math.inf, False, {}),
        (_SCORED, -math.inf, False, {}),
        (_INVALID, None, False, {}),
        # A 0 would read as a genuinely fast or cheap execution.
        (_INVALID, 0.0, True, {}),
        (_SKIPPED, 0.0, False, {}),
        (_SKIPPED, None, True, {}),
        (_UNAVAILABLE, None, False, {}),
        (_UNAVAILABLE, None, True, _REASON),
        (_UNAVAILABLE, 0.0, False, _REASON),
    ],
    ids=[
        "scored_without_value",
        "scored_with_error",
        "scored_nan",
        "scored_inf",
        "scored_minus_inf",
        "invalid_without_error",
        "invalid_with_zero",
        "skipped_with_zero",
        "skipped_with_error",
        "unavailable_without_reason",
        "unavailable_with_error",
        "unavailable_with_zero",
    ],
)
def test_a_result_breaking_the_status_invariants_is_rejected(
    status: MetricStatus, value: float | None, with_error: bool, detail: dict[str, str]
) -> None:
    """Only ``scored`` carries a finite value; only ``invalid`` carries an error."""
    with pytest.raises(ValidationError):
        PerformanceResult(
            status=status,
            value=value,
            error=_error() if with_error else None,
            detail=detail,
            metadata=_META,
        )


@pytest.mark.parametrize(
    ("status", "value", "with_error", "detail"),
    [
        # Unbounded, unlike a quality score capped at 100.
        (_SCORED, 10_432.0, False, {}),
        (_INVALID, None, True, {}),
        (_SKIPPED, None, False, {}),
        (_UNAVAILABLE, None, False, _REASON),
    ],
    ids=["scored", "invalid", "skipped", "unavailable"],
)
def test_a_result_keeping_the_status_invariants_is_built(
    status: MetricStatus, value: float | None, with_error: bool, detail: dict[str, str]
) -> None:
    error = _error() if with_error else None

    result = PerformanceResult(status=status, value=value, error=error, detail=detail, metadata=_META)

    assert (result.value, result.error, dict(result.detail)) == (value, error, detail)


_CALL = CallRecord(kind=CallKind.model, scope=CallScope.agent, duration_ms=120.0)


@pytest.mark.parametrize(
    ("instance", "field", "value"),
    [
        (PerformanceResult(status=_SCORED, value=1.0, metadata=_META), "value", 2.0),
        (_META, "unit", MetricUnit.count),
        (_CALL, "duration_ms", 1.0),
        (ExecutionTelemetry(wall_clock_ms=5.0), "wall_clock_ms", 6.0),
    ],
    ids=["PerformanceResult", "PerformanceMetadata", "CallRecord", "ExecutionTelemetry"],
)
def test_results_metadata_and_telemetry_are_frozen(instance: BaseModel, field: str, value: object) -> None:
    """A result or its input cannot change after a metric has read it."""
    with pytest.raises(ValidationError):
        setattr(instance, field, value)


def test_to_json_sorts_keys() -> None:
    result = PerformanceResult(
        status=MetricStatus.scored, value=1.5, detail={"z": 1, "a": 2}, metadata=_META
    )
    rendered = result.to_json()
    assert rendered.index('"a"') < rendered.index('"z"')


def test_round_trip_preserves_the_value_and_refuses_non_finite() -> None:
    """A value survives ``to_dict`` unchanged, and ``to_json`` still refuses NaN.

    Construction already rejects non-finite values, so the serializer's own guard is
    only reachable through ``model_construct``, which skips validation. Testing the
    round trip alone would leave that documented failure mode unexercised.
    """
    result = PerformanceResult(status=MetricStatus.scored, value=0.1, metadata=_META)

    data = result.to_dict()
    assert data["value"] == 0.1
    assert PerformanceResult.model_validate(data) == result

    unvalidated = PerformanceResult.model_construct(
        status=MetricStatus.scored,
        value=math.nan,
        metadata=_META,
        detail=None,
        error=None,
    )
    with pytest.raises(ValueError):
        unvalidated.to_json()


# ── metadata identity: a `performance.` key and a known kind ──

def _metadata(**overrides: object) -> PerformanceMetadata:
    fields: dict[str, object] = {
        "key": "performance.latency.wall_clock",
        "label": "End-to-End Latency",
        "kind": "duration",
        "primary_measure": "wall_clock",
        "unit": MetricUnit.milliseconds,
        "direction": MetricDirection.lower_is_better,
        "rollup": "mean",
    }
    fields.update(overrides)
    return PerformanceMetadata(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        # Namespaced keys cannot collide with the quality suite's keys.
        {"key": "latency.wall_clock"},
        {"key": ""},
        {"kind": "unknown_shape"},
    ],
    ids=["key_outside_the_namespace", "empty_key", "unknown_kind"],
)
def test_metadata_with_an_invalid_identity_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _metadata(**overrides)


@pytest.mark.parametrize("kind", ["duration", "tokens", "cost", "count", "rate"])
def test_every_known_aggregation_shape_is_accepted(kind: str) -> None:
    assert _metadata(kind=kind).kind == kind


# ── rollup declaration ──
# `mean`: the run averages its entries. `pooled`: the value is a share of things counted
# inside the entry (a pass/fail metric counts one item), so the run adds up every entry's
# counts and divides; a scored pooled result carries those counts. `macro`: the run sums
# each class's counts, then averages the classes' F1; a scored macro result carries its
# per-class counts. There is no default: a metric without one cannot be built.

_POOLED = {
    "performance.reliability.completion_rate",
    "performance.reliability.tool_error_rate",
    "classification.micro_prf",
    "classification.subset_accuracy",
    "classification.vocabulary_validity",
    "json.field_accuracy",
    "json.schema_validity",
    "prose.exact_match",
    "stix.entity_prf",
    "stix.entity_prf.aligned",
    "stix.entity_prf.aligned.closed",
    "stix.entity_prf.soft",
    "stix.envelope_validity",
    "stix.relationship_prf",
    "stix.relationship_prf.aligned",
    "stix.relationship_prf.aligned.closed",
    "stix.semantic_validity",
}
_MACRO = {"classification.macro_prf", "stix.entity_prf.by_type"}


@pytest.fixture
def fresh_registry():
    registry.reset_cache()
    yield
    registry.reset_cache()


def test_every_package_metric_declares_its_rollup(fresh_registry) -> None:
    definitions = registry.registry().definitions
    assert definitions
    assert {d.key for d in definitions if d.rollup not in {"mean", "pooled", "macro"}} == set()


def test_each_metric_rolls_up_as_its_definition_says(fresh_registry) -> None:
    """The roll-ups: a share of items or a pass/fail is pooled, a
    mean F1 over classes is macro, the rest is averaged."""
    assert registry.registry().pooled_keys == _POOLED
    assert registry.registry().macro_keys == _MACRO
    assert {m.key for m in METRIC_METADATA if m.rollup is MetricRollup.pooled} == {
        key for key in _POOLED if key.startswith("performance.")
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda: MetricMetadata(
            key="q.k", label="q", kind="quality", primary_measure="v",
            direction=MetricDirection.higher_is_better,
        ),
        lambda: PerformanceMetadata(
            key="performance.k", label="p", kind="count", primary_measure="v",
            unit=MetricUnit.count, direction=MetricDirection.lower_is_better,
        ),
    ],
    ids=["quality", "performance"],
)
def test_a_metric_without_a_rollup_cannot_be_declared(build) -> None:
    with pytest.raises(ValidationError, match="rollup"):
        build()


def test_the_pooled_rates_carry_the_counts_their_value_is_the_ratio_of() -> None:
    def tool(outcome: CallOutcome) -> CallRecord:
        return CallRecord(kind=CallKind.tool, scope=CallScope.agent, name="check", outcome=outcome)

    model = CallRecord(kind=CallKind.model, scope=CallScope.agent, outcome=CallOutcome.success)
    telemetry = ExecutionTelemetry(
        calls=(model, tool(CallOutcome.error), tool(CallOutcome.success), tool(CallOutcome.error))
    )

    results = reliability(telemetry)

    tool_errors = results["performance.reliability.tool_error_rate"]
    assert (tool_errors.detail[POOLED_NUMERATOR], tool_errors.detail[POOLED_DENOMINATOR]) == (2, 3)
    assert tool_errors.value == pytest.approx(2 / 3)
    completion = results["performance.reliability.completion_rate"]
    assert (completion.detail[POOLED_NUMERATOR], completion.detail[POOLED_DENOMINATOR]) == (2, 4)
    assert completion.value == pytest.approx(0.5)


def _pooled_result(value: float, **detail) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.scored,
        value=value,
        detail=detail,
        metadata=PerformanceMetadata(
            key="performance.fake.rate", label="rate", kind="rate", primary_measure="rate",
            unit=MetricUnit.ratio, direction=MetricDirection.lower_is_better,
            rollup=MetricRollup.pooled,
        ),
    )


@pytest.mark.parametrize(
    ("value", "detail"),
    [
        (0.5, {}),
        (0.5, {POOLED_NUMERATOR: 1}),
        (0.5, {POOLED_NUMERATOR: 1, POOLED_DENOMINATOR: 0}),
        (1.5, {POOLED_NUMERATOR: 3, POOLED_DENOMINATOR: 2}),
        (0.5, {POOLED_NUMERATOR: True, POOLED_DENOMINATOR: 2}),
        (0.5, {POOLED_NUMERATOR: -1.0, POOLED_DENOMINATOR: -2}),
        (0.5, {POOLED_NUMERATOR: float("nan"), POOLED_DENOMINATOR: 2}),
        (0.4, {POOLED_NUMERATOR: 1, POOLED_DENOMINATOR: 2}),
    ],
    ids=["no_counts", "no_denominator", "zero_denominator", "more_than_all", "bool", "negative", "nan", "value_mismatch"],
)
def test_a_scored_pooled_result_requires_consistent_counts(value, detail) -> None:
    with pytest.raises(ValidationError):
        _pooled_result(value, **detail)


def test_a_scored_pooled_result_with_consistent_counts_is_accepted() -> None:
    assert _pooled_result(0.5, **{POOLED_NUMERATOR: 1, POOLED_DENOMINATOR: 2}).value == 0.5


def test_pooled_counts_may_be_weighted() -> None:
    """A weighted share (fields' credit over their weights) is not a whole number."""
    check_rollup_counts(
        MetricRollup.pooled, True, 75.0, {POOLED_NUMERATOR: 1.5, POOLED_DENOMINATOR: 2.0}, scale=100.0
    )


@pytest.mark.parametrize(
    "classes",
    [None, [], {"a": {"tp": 1, "fp": 0}}, {"a": {"tp": 1.0, "fp": 0, "fn": 0}}, {"a": {"tp": -1, "fp": 0, "fn": 0}}],
    ids=["missing", "not_a_mapping", "missing_count", "float_count", "negative_count"],
)
def test_a_scored_macro_result_requires_its_class_counts(classes) -> None:
    detail = {} if classes is None else {MACRO_CLASSES: classes}
    with pytest.raises(ValueError):
        check_rollup_counts(MetricRollup.macro, True, 50.0, detail, scale=100.0)


def test_a_scored_macro_result_with_class_counts_is_accepted() -> None:
    check_rollup_counts(
        MetricRollup.macro, True, 50.0, {MACRO_CLASSES: {"a": {"tp": 1, "fp": 0, "fn": 0, "f1": 100.0}, "b": {"tp": 0, "fp": 1, "fn": 0}}}, scale=100.0
    )


def test_counts_are_checked_on_the_result_s_own_scale() -> None:
    """A quality value is a 0-100 score, so its pooled value is 100 times the ratio."""
    counts = {POOLED_NUMERATOR: 1, POOLED_DENOMINATOR: 4}
    check_rollup_counts(MetricRollup.pooled, True, 25.0, counts, scale=100.0)
    with pytest.raises(ValueError):
        check_rollup_counts(MetricRollup.pooled, True, 0.25, counts, scale=100.0)


def test_nothing_expected_and_nothing_given_is_a_full_quality_score_only() -> None:
    """0 of 0 is an empty answer, correct on a quality score; a rate has no value."""
    empty = {POOLED_NUMERATOR: 0, POOLED_DENOMINATOR: 0}
    check_rollup_counts(MetricRollup.pooled, True, 100.0, empty, scale=100.0)
    with pytest.raises(ValueError):
        check_rollup_counts(MetricRollup.pooled, True, 0.0, empty, scale=100.0)
    with pytest.raises(ValueError):
        check_rollup_counts(MetricRollup.pooled, True, 1.0, empty)


def test_an_unscored_or_averaged_result_needs_no_counts() -> None:
    check_rollup_counts(MetricRollup.pooled, False, None, {})
    check_rollup_counts(MetricRollup.mean, True, 0.5, {})


# ── no I/O in the performance package ──
# The package reports the durations it is given but never reads a clock or does I/O,
# enforced statically. Reading telemetry from disk is the caller's job, outside the package.

_PERFORMANCE = pathlib.Path(str(performance_pkg.__file__)).parent

# Reading any of these would make a "pure" metric non-deterministic or I/O-bearing.
_FORBIDDEN_ROOTS = frozenset(
    {
        "time",
        "datetime",
        "random",
        "socket",
        "requests",
        "httpx",
        "os",
        "pathlib",
        "urllib",
        "subprocess",
        "threading",
        "asyncio",
    }
)


def _modules() -> list[pathlib.Path]:
    return sorted(_PERFORMANCE.rglob("*.py"))


def _imported_roots(path: pathlib.Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_package_has_modules_to_guard() -> None:
    assert _modules(), "expected performance modules to be discoverable"


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_performs_no_io(path: pathlib.Path) -> None:
    offenders = sorted(_imported_roots(path) & _FORBIDDEN_ROOTS)
    assert offenders == [], f"{path.name} imports I/O or clock module(s): {offenders}"


def test_no_module_reads_the_clock() -> None:
    """Catch a clock read the import scan misses: one through a relative or ``app.*`` import."""
    for path in _modules():
        source = path.read_text(encoding="utf-8")
        for banned in ("perf_counter", "monotonic(", "time()", "utcnow", "now("):
            assert banned not in source, f"{path.name} appears to read a clock: {banned}"
