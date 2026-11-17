"""The metric catalogue: the family-to-sub-package mapping, advertised families against
computed families, the labels of legacy metric keys, and the unit of each performance metric.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

from app.services.evaluation.metric_package import families as families_ids
from app.services.evaluation.metric_package import registry, units
from app.services.evaluation.metric_package.catalog import (
    metric_catalog,
    performance_catalog,
    quality_catalog,
)
from app.services.evaluation.metric_package.directions import (
    performance_directions,
    quality_directions,
)
from app.services.evaluation.metric_package.families import (
    FAMILY_IDS,
    list_families,
)
from app.services.evaluation.metric_package.registry import FAMILY_MODULES

# ── Family to sub-package mapping ──
# The catalogue maps a stored ``Evaluation.family`` (an uppercase canonical id) onto the
# sub-package holding that family's quality metrics. A lookup miss empties the quality
# half of the selector while scoring still computes the metrics; turning "Use all" off
# then writes ``overall_quality_metrics = []`` and zeroes Overall Quality. The mapping is
# pinned to the family catalogue, not to a second hardcoded list, so a rename upstream
# fails here.


_UNITS = {"milliseconds", "count", "tokens", "currency", "ratio"}


@pytest.mark.parametrize("family", sorted(FAMILY_IDS))
def test_implemented_families_offer_quality_metrics(family: str) -> None:
    """A family the package computes metrics for must be able to compose them.

    Otherwise a mapping miss reads as "this family measures no metric of this
    kind yet".
    """
    entries = quality_catalog(family)
    assert entries, f"{family} computes deterministic metrics but offers none to compose"
    assert all(e["kind"] == "quality" for e in entries)


@pytest.mark.parametrize(
    "family", [None, "NOT_A_FAMILY", "stix_bundle"], ids=["unset", "unknown", "lowercase_id"]
)
def test_an_unknown_or_unset_family_offers_no_quality_metric(family) -> None:
    """No family still yields the family-independent performance half.

    The lookup is case-sensitive: coercing case would hide a drift, and the stored value
    is written from ``FAMILY_IDS`` verbatim.
    """
    assert quality_catalog(family) == []
    assert metric_catalog(family) == performance_catalog()
    assert quality_catalog("STIX_BUNDLE"), "the canonical id must still resolve"


@pytest.mark.parametrize("family", FAMILY_IDS)
def test_every_family_advertises_the_performance_half(family: str) -> None:
    """Performance metrics measure the call, not the output, so they are universal."""
    performance = [e for e in metric_catalog(family) if e["kind"] == "performance"]
    assert performance == performance_catalog()


def _fake_module(name: str, **metadata) -> types.ModuleType:
    """A package-like module holding *metadata*, as the registry's walk reads one."""
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    for attr, value in metadata.items():
        setattr(module, attr, value)
    return module


# A name inside STIX_BUNDLE's sub-package, so the walk files its metrics under that family.
_FAKE_FAMILY_MODULE = "app.evaluation_metrics.quality.families.stix_bundle._fake_for_test"


def _use_registry_of(monkeypatch, quality_root=None, performance_root=None) -> None:
    """Make every view read the registry built from these roots instead of the package."""
    from app.services.evaluation.metric_package import registry

    quality_root = quality_root or _fake_module("fake_quality_root")
    performance_root = performance_root or _fake_module("fake_performance_root")
    monkeypatch.setattr(
        registry, "_collect", lambda: registry.build_registry(quality_root, performance_root)
    )
    registry.reset_cache()


def test_an_unoriented_performance_metric_is_dropped_before_the_catalogue(monkeypatch) -> None:
    """An unoriented performance metric cannot be normalised, so it is never offered.

    Unlike `quality_catalog`, which keeps it with a null direction. Every shipped metric
    declares a valid direction, so the case is built.
    """
    from app.evaluation_metrics.performance.results import (
        MetricUnit,
        PerformanceMetadata,
    )
    from app.services.evaluation.metric_package import registry

    class LoosePerformanceMetadata(PerformanceMetadata):
        """`direction` widened to `str` so an invalid value can be constructed.

        The real model's `MetricDirection` enum is the first line of defence;
        this exercises the registry's filter, the second.
        """

        direction: str  # type: ignore[assignment]

    def metric(key: str, direction: str) -> LoosePerformanceMetadata:
        return LoosePerformanceMetadata(
            key=key, label=key, kind="count", primary_measure="value",
            unit=MetricUnit.count, direction=direction, rollup="mean",
        )

    _use_registry_of(
        monkeypatch,
        performance_root=_fake_module(
            "fake_perf_root",
            _ORIENTED=metric("performance.fake.oriented", "lower_is_better"),
            _UNORIENTED=metric("performance.fake.unoriented", "sideways"),
        ),
    )
    try:
        offered = {entry["key"] for entry in performance_catalog()}
    finally:
        registry.reset_cache()

    assert "performance.fake.oriented" in offered, "the synthetic module was not walked at all"
    assert "performance.fake.unoriented" not in offered, (
        "an unoriented performance metric is offered, with a direction no headline can use"
    )


@pytest.mark.parametrize(
    ("key", "direction", "unit"),
    [
        ("performance.latency.wall_clock", "lower_is_better", "milliseconds"),
        ("performance.latency.ttft", "lower_is_better", "milliseconds"),
        ("performance.counts.model_calls", "lower_is_better", "count"),
        ("performance.counts.tool_calls", "lower_is_better", "count"),
        ("performance.counts.retries", "lower_is_better", "count"),
        ("performance.tokens.total", "lower_is_better", "tokens"),
        ("performance.cost.llm", "lower_is_better", "currency"),
        ("performance.reliability.completion_rate", "higher_is_better", "ratio"),
        ("performance.reliability.tool_error_rate", "lower_is_better", "ratio"),
    ],
    ids=lambda value: value.removeprefix("performance.") if value.startswith("performance.") else None,
)
def test_each_performance_metric_is_offered_with_its_direction_and_unit(key, direction, unit) -> None:
    """The catalogue serves each performance metric with the package's direction and unit.

    The direction orients the headline and the unit formats the value; neither is ever null.
    """
    entry = next(e for e in performance_catalog() if e["key"] == key)
    assert entry["kind"] == "performance"
    assert entry["direction"] == direction
    assert entry["unit"] == unit


def test_a_performance_metric_is_offered_under_the_package_label() -> None:
    """Named as the package names it, as a quality metric is, never from its key."""
    from app.evaluation_metrics.performance.metrics.latency import WALL_CLOCK_META

    entry = next(e for e in performance_catalog() if e["key"] == WALL_CLOCK_META.key)
    assert entry["label"] == WALL_CLOCK_META.label


def test_every_oriented_catalogue_metric_can_compose_a_headline() -> None:
    """A metric offered *with* an orientation must be one aggregation can use.

    The catalogue and the direction registry are two independent filters over
    the same package walk. A metric the catalogue offers but the registry
    cannot orient would be selected, scored, and silently left out of Overall
    Quality.

    The containment is one-directional: the registry also holds keys the
    catalogue does not offer: a metric declared outside every family
    sub-package belongs to no family.
    """
    registry = quality_directions()

    for family in FAMILY_IDS:
        for metric in quality_catalog(family):
            if metric["direction"] is None:
                continue
            assert metric["key"] in registry, (
                f"{family} offers {metric['key']} with a direction, but the registry "
                "has no orientation for it, so selecting it would silently drop it "
                "from Overall Quality"
            )
            assert registry[metric["key"]] == metric["direction"], (
                f"{metric['key']} is offered as {metric['direction']} but composes "
                f"the headline as {registry[metric['key']]}"
            )


def test_the_two_filters_agree_on_a_family_the_suite_does_not_ship(monkeypatch) -> None:
    """Mutation-sensitive counterpart to the test above, over a synthetic family.

    Every shipped metric is oriented, so the case is built: an unoriented metric is
    *offered* with a null direction but *withheld* from the directions, and a metric the
    platform hides reaches neither.
    """
    from app.evaluation_metrics.quality.results import MetricMetadata
    from app.services.evaluation.metric_package import registry

    class LooseMetricMetadata(MetricMetadata):
        """`direction` widened to `str` so an unoriented metric can be built."""

        direction: str  # type: ignore[assignment]

    def metric(key: str, direction: str) -> LooseMetricMetadata:
        return LooseMetricMetadata(
            key=key, label=key, kind="quality", primary_measure="value", direction=direction, rollup="mean"
        )

    _use_registry_of(
        monkeypatch,
        quality_root=_fake_module(
            _FAKE_FAMILY_MODULE,
            _ORIENTED=metric("fake.oriented", "higher_is_better"),
            _UNORIENTED=metric("fake.unoriented", "sideways"),
            _HIDDEN=metric("prose.required_phrase_coverage", "higher_is_better"),
        ),
    )
    try:
        offered = {m["key"]: m["direction"] for m in quality_catalog(families_ids.STIX_BUNDLE)}
        directions_registry = quality_directions()
    finally:
        registry.reset_cache()

    assert "fake.oriented" in offered, "the synthetic family module was not walked at all"

    oriented = {k: v for k, v in offered.items() if v is not None}
    assert set(oriented) <= set(directions_registry), (
        "a metric is offered with a direction that the registry cannot resolve, so "
        f"selecting it would drop it from the headline: {sorted(set(oriented) - set(directions_registry))}"
    )
    assert offered["fake.unoriented"] is None, (
        "an unoriented metric reached the catalogue with a direction, so it would be "
        "offered as composable while the registry withholds it"
    )
    assert "fake.unoriented" not in directions_registry
    assert "prose.required_phrase_coverage" not in offered, (
        "a hidden metric is selectable; no evaluation can configure it, so it "
        "must never be offered"
    )
    assert "prose.required_phrase_coverage" not in directions_registry


# --- default_contributor -------------------------------------------------------


def test_the_default_headline_counts_exactly_the_answer_measures() -> None:
    """Each family's default contributors, and no other metric reclassified.

    A validity check repeats the 0 an invalid answer already scores, and a stricter or
    redundant view doubles a default's weight, so both are opt-in. Time to first token is
    the one non-default performance metric: on a short answer it is nearly the
    latency, so counting both by default weighs the latency twice.
    """
    from app.services.evaluation.metric_package.catalog import performance_catalog

    defaults = {
        family: {e["key"] for e in quality_catalog(family) if e["default_contributor"]}
        for family in sorted(FAMILY_IDS)
    }
    assert defaults == {
        families_ids.STIX_BUNDLE: {"stix.entity_prf", "stix.relationship_prf"},
        families_ids.STRUCTURED_JSON: {"json.field_accuracy"},
        families_ids.CLASSIFICATION: {
            "classification.micro_prf",
            "classification.macro_prf",
            "classification.samples_f1",
        },
        families_ids.PROSE: {"prose.token_prf"},
    }
    assert {e["key"] for e in performance_catalog() if not e["default_contributor"]} == {
        "performance.latency.ttft"
    }


def test_every_prose_similarity_metric_is_offered_with_a_label_and_a_direction() -> None:
    """Each of the three prose similarity metrics has a readable label and a direction.

    An unoriented metric cannot compose a headline at all.
    """
    from app.services.evaluation.metric_package.catalog import quality_catalog

    by_key = {m["key"]: m for m in quality_catalog("PROSE")}
    for key in ("prose.exact_match", "prose.token_prf", "prose.rouge_l"):
        entry = by_key[key]
        assert entry["label"] and entry["label"] != key, f"{key} has no readable label"
        assert entry["direction"] == "higher_is_better", f"{key} is not oriented"
        assert entry["kind"] == "quality"


def test_every_catalogue_entry_survives_the_api_response_model() -> None:
    """Every field the catalogue emits survives the endpoint's response model.

    `GET /evaluations/{id}/metric-catalog` declares `response_model=list[MetricCatalogEntry]`,
    and a Pydantic response model drops whatever it does not declare. The selector tests
    `default_contributor` with `!== false`, so an absent field would make every metric a
    default contributor; the run page formats a value by its `unit`.
    """
    from app.api.v1.schemas.evaluation import MetricCatalogEntry

    entries = performance_catalog()
    for family in sorted(FAMILY_IDS):
        entries += quality_catalog(family)
    assert entries
    for entry in entries:
        served = MetricCatalogEntry(**entry).model_dump()
        assert served == entry, (
            f"{entry['key']} changes through the response model; declare every field the "
            "catalogue emits on MetricCatalogEntry."
        )
        if entry["kind"] == "quality":
            assert served["unit"] is None, entry["key"]
        else:
            assert served["unit"] in _UNITS, entry["key"]

    # The metric whose flag is False must survive as False, not default to True.
    exact_match = next(m for m in quality_catalog("PROSE") if m["key"] == "prose.exact_match")
    assert MetricCatalogEntry(**exact_match).model_dump()["default_contributor"] is False


# ── Advertised families against computed families ──
# ``FAMILY_IDS`` (``families.py``) says which families can be scored and
# ``FAMILY_MODULES`` (``registry.py``) which have selectable metrics. Drift between them is
# silent: a run cannot tell "never computed" from "no data". Both directions are pinned:
# advertised but not computable is a selection no run can fulfil; computable but not
# advertised is a metric computed and stored that no author can select.


def test_the_two_declarations_describe_the_same_families() -> None:
    assert set(FAMILY_MODULES) == set(FAMILY_IDS), (
        "registry.FAMILY_MODULES and families.FAMILY_IDS disagree. "
        "Only in the catalogue: "
        f"{sorted(set(FAMILY_MODULES) - set(FAMILY_IDS))}; "
        "only in the declared families: "
        f"{sorted(set(FAMILY_IDS) - set(FAMILY_MODULES))}"
    )


# --- PROSE: wired, and both halves of the gate still intact -------------------------


def test_prose_is_wired_through_the_adapter() -> None:
    """PROSE computes because the adapter supplies a policy, not because the suite defaults one.

    * a bare ``FamilyConfig`` is a configuration error: the suite's gate holds;
    * the adapter's config yields the five prose metrics.

    Keep both assertions: if the suite started defaulting the policy, prose would
    be scored under a normalization nobody chose, and only the first catches it.
    """
    from app.evaluation_metrics.core.errors import ConfigurationError
    from app.evaluation_metrics.quality import Family, FamilyConfig, ProseConfig, run_for_family
    from app.services.evaluation.metric_package.quality_adapter import (
        PROSE_NORMALIZATION_POLICY,
    )

    with pytest.raises(ConfigurationError):
        run_for_family("a reference", "a prediction", Family.PROSE, FamilyConfig())

    configured = run_for_family(
        "a reference",
        "a prediction",
        Family.PROSE,
        FamilyConfig(prose=ProseConfig(policy=PROSE_NORMALIZATION_POLICY)),
    )
    assert set(configured) == {
        "prose.exact_match",
        "prose.token_prf",
        "prose.rouge_l",
        "prose.required_phrase_coverage",
        "prose.forbidden_phrase_absence",
    }, "the adapter's prose config no longer produces the five prose metrics."


_STIX_BUNDLE = {
    "type": "bundle",
    "id": "bundle--00000000-0000-4000-8000-000000000000",
    "objects": [
        {
            "type": "identity",
            "spec_version": "2.1",
            "id": "identity--00000000-0000-4000-8000-000000000001",
            "name": "ACME",
            "identity_class": "organization",
        }
    ],
}

# One well-formed entry per family: (reference, prediction, dataset references, schema).
_WELL_FORMED_ENTRY = {
    families_ids.STIX_BUNDLE: (json.dumps(_STIX_BUNDLE), json.dumps(_STIX_BUNDLE), None, None),
    families_ids.STRUCTURED_JSON: (
        '{"name": "a"}',
        '{"name": "a"}',
        None,
        {"type": "object", "properties": {"name": {"type": "string"}}},
    ),
    families_ids.CLASSIFICATION: ('["malware"]', '["malware"]', None, None),
    families_ids.PROSE: ("the actor used phishing", "the actor used phishing", None, None),
}


def _keys_a_run_stores(family: str) -> set[str]:
    from types import SimpleNamespace

    from app.services.evaluation.metric_package.quality_adapter import (
        build_quality_config,
        run_quality,
    )

    reference, prediction, dataset_refs, schema = _WELL_FORMED_ENTRY[family]
    config = build_quality_config(
        family, SimpleNamespace(default_output_schema=None), dataset_refs or [reference]
    )
    outcomes = run_quality(
        family=family,
        reference=reference,
        prediction=prediction,
        output_schema=schema,
        config=config,
    )
    keys = {o.key for o in outcomes}
    # A STIX backend that cannot load degrades to this one outcome: an environment
    # problem, not a catalogue drift, so said as such. Never skipped, so the guard cannot
    # silently stop running.
    if keys == {"stix.backend"}:
        pytest.fail(f"the STIX backend did not load here: {outcomes[0].detail}")
    return keys


@pytest.mark.parametrize("family", sorted(FAMILY_IDS))
def test_every_advertised_key_is_one_a_run_stores(family: str) -> None:
    """What the UI offers equals what a run of the family stores, both ways.

    Compared with ``run_quality``'s output, not with the declared metadata: a metric
    declared but never routed (``classification.example.set_prf``) was
    advertised, selected by default, and reported "failed, a re-run may succeed" on every
    run with a manual selection.
    """
    stored = _keys_a_run_stores(family)
    advertised = {f["id"]: set(f["metric_keys"]) for f in list_families()}[family]
    offered = {metric["key"] for metric in quality_catalog(family)}
    assert offered == stored, (
        f"{family}: the selector offers {sorted(offered - stored)} that no run stores, "
        f"and a run stores {sorted(stored - offered)} the selector does not offer."
    )
    assert advertised == stored, (
        f"{family}: families._CATALOG advertises {sorted(advertised - stored)} that no run "
        f"stores, and leaves out {sorted(stored - advertised)}."
    )


# ── Labels of legacy metric keys ──
# Legacy performance keys are not written, but stored rows still hold them, and
# `metricDisplayName` falls back to the raw key, so a dropped label degrades a run's
# metrics panel to raw identifiers without failing. No label carries a `(perf)` or `(legacy)`
# suffix: no result holds both a package key and its legacy counterpart.


FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend/src"
CONSTANTS = FRONTEND_ROOT / "lib/constants.ts"

# Every key the platform has ever written and may still hold rows for, with the
# exact label a stored row must keep rendering: the original label and unit, so
# `Cost (USD)` cannot drift to `Cost`.
LEGACY_METRIC_LABELS = {
    "ttft_seconds": "TTFT (s)",
    "agent_seconds": "Agent Time (s)",
    "judge_seconds": "Judge Time (s)",
    "total_seconds": "Total Time (s)",
    "input_tokens": "Input Tokens",
    "output_tokens": "Output Tokens",
    "llm_iterations": "LLM Iterations",
    "tool_calls": "Tool Calls",
    "cost_usd": "Cost (USD)",
    "schema_score": "Schema Score",
}

# Replaces `total_seconds`, which stays in the legacy set above because
# rows carrying it are never rewritten; a rescored result can hold both.
CURRENT_METRIC_LABELS = {
    "run_duration_seconds": "Run Duration (s)",
}

# Package keys with a pinned label. Other `performance.*` keys the package
# declares carry no label here.
PACKAGE_METRIC_LABELS = {
    "performance.counts.tool_calls": "Tool Calls",
    "performance.counts.retries": "Retries",
}


def _frontend_table(name: str) -> dict[str, str]:
    """The `constants.ts` table `name` parsed into `{key: text}`."""
    source = CONSTANTS.read_text(encoding="utf-8")
    start = source.index(f"export const {name}")
    # The table closes on the first line that starts with `}` (`} as const ...`).
    body = source[start : source.index("\n}", start)]
    return {
        (m.group("quoted") or m.group("bare")): m.group("label")
        for m in re.finditer(
            r'^\s*(?:"(?P<quoted>[^"]+)"|(?P<bare>[A-Za-z_][\w]*))\s*:\s*"(?P<label>[^"]*)"',
            body,
            re.MULTILINE,
        )
    }


@pytest.fixture(scope="module")
def labels() -> dict[str, str]:
    """`METRIC_KEY_LABELS` parsed into `{key: label}`."""
    return _frontend_table("METRIC_KEY_LABELS")


@pytest.mark.parametrize(("key", "label"), sorted(LEGACY_METRIC_LABELS.items()))
def test_legacy_labels_survive(labels, key, label):
    """A stored row must keep rendering its original label and unit."""
    assert key in labels, (
        f"{key!r} lost its label; stored runs would render it raw"
    )
    assert labels[key] == label, (
        f"{key!r} label drifted from {label!r} to {labels[key]!r}; a stored row "
        "would render under a different name or lose its unit"
    )


def test_no_label_carries_a_disambiguation_suffix(labels):
    """No label carries a `(perf)` or `(legacy)` disambiguation suffix."""
    marked = {k: v for k, v in labels.items() if re.search(r"\((perf|legacy)\)", v)}
    assert not marked, f"disambiguation suffix left on {marked}"


@pytest.mark.parametrize(("key", "label"), sorted(PACKAGE_METRIC_LABELS.items()))
def test_package_labels_are_pinned(labels, key, label):
    """Each package key rendered by the run detail panel keeps its label."""
    assert labels.get(key) == label, (
        f"{key!r} is rendered by the metrics panel and must be labelled {label!r}, "
        f"got {labels.get(key)!r}"
    )


@pytest.mark.parametrize(("key", "label"), sorted(CURRENT_METRIC_LABELS.items()))
def test_currently_written_labels_are_pinned(labels, key, label):
    """A key the platform writes today must render as a name, not a raw key."""
    assert labels.get(key) == label, (
        f"{key!r} is written by the metrics logger and must be labelled {label!r}, "
        f"got {labels.get(key)!r}"
    )



# ── One name per package metric ──
# The pages name a metric from `METRIC_KEY_LABELS` (translated), the API from the
# package's label: they must say the same, or a metric has two names again. Every
# package metric also carries the one-line description its name shows as a tooltip.


def test_every_package_metric_has_the_package_label_on_every_page(labels):
    package = {d.key: d.label for d in registry.registry().definitions}
    mismatched = {
        key: {"package": label, "pages": labels.get(key)}
        for key, label in sorted(package.items())
        if labels.get(key) != label
    }
    assert not mismatched, (
        "the pages and the package name these metrics differently; make "
        f"METRIC_KEY_LABELS in {CONSTANTS} and the package label match: {mismatched}"
    )


def test_every_package_metric_says_what_it_measures():
    descriptions = _frontend_table("METRIC_KEY_DESCRIPTIONS")
    missing = sorted(d.key for d in registry.registry().definitions if not descriptions.get(d.key))
    assert not missing, f"no METRIC_KEY_DESCRIPTIONS entry in {CONSTANTS} for {missing}"


# ── How a metric reads on one entry ──
# Only the classification F1s read differently on a single entry: micro, macro and samples
# combine entries, so the entry shows one "Label F1". The pages name it from
# `METRIC_ENTRY_LABELS` (translated), the API from the package: they must say the same.


def test_only_the_classification_f1s_read_differently_on_one_entry():
    differing = {
        d.key: (d.shown_on_entry, d.entry_label)
        for d in registry.registry().definitions
        if not d.shown_on_entry or d.entry_label is not None
    }
    assert differing == {
        "classification.micro_prf": (True, "Label F1"),
        "classification.macro_prf": (False, None),
        "classification.samples_f1": (False, None),
    }


def test_a_metric_declared_two_ways_is_shown_on_an_entry_under_its_own_name(monkeypatch, caplog):
    """Declarations that disagree fall back to the default, reported, never to the walk order."""
    from app.evaluation_metrics.core.contracts import MetricDirection, MetricRollup
    from app.evaluation_metrics.quality.results import MetricMetadata
    from app.services.evaluation.metric_package.entry_view import EntryView, metric_entry_view

    def metric(key: str, **entry) -> MetricMetadata:
        return MetricMetadata(
            key=key, label=key, kind="quality", primary_measure="value",
            direction=MetricDirection.higher_is_better, rollup=MetricRollup.mean, **entry,
        )

    _use_registry_of(
        monkeypatch,
        quality_root=_fake_module(
            _FAKE_FAMILY_MODULE,
            _HIDDEN=metric("fake.hidden", shown_on_entry=False),
            _RENAMED=metric("fake.renamed", entry_label="Entry name"),
            _CONFLICT_A=metric("fake.conflict", shown_on_entry=False),
            _CONFLICT_B=metric("fake.conflict"),
        ),
    )
    try:
        assert metric_entry_view("fake.hidden") == EntryView(shown=False)
        assert metric_entry_view("fake.renamed") == EntryView(label="Entry name")
        assert metric_entry_view("fake.conflict") == EntryView()
        assert metric_entry_view("fake.unknown") == EntryView()
        assert "fake.conflict with conflicting entry views" in caplog.text
    finally:
        registry.reset_cache()


def test_every_entry_name_is_the_package_one_and_says_what_it_measures():
    package = {d.key: d.entry_label for d in registry.registry().definitions if d.entry_label}
    assert _frontend_table("METRIC_ENTRY_LABELS") == package, (
        f"METRIC_ENTRY_LABELS in {CONSTANTS} and the package's entry labels differ"
    )
    descriptions = _frontend_table("METRIC_ENTRY_DESCRIPTIONS")
    missing = sorted(key for key in package if not descriptions.get(key))
    assert not missing, f"no METRIC_ENTRY_DESCRIPTIONS entry in {CONSTANTS} for {missing}"


# ── The family copy the pages translate ──
# The pages translate a family's label and description with the server's English text as
# the catalog key, and `validate-i18n` only requires a translation of the copy
# `FAMILY_CATALOG_COPY` lists. A sentence the server changes without it reads in English
# in every language (the STIX description once did).

FAMILIES_TS = FRONTEND_ROOT / "lib/evaluation-families.ts"


def test_every_family_text_the_server_serves_is_one_the_pages_translate():
    source = FAMILIES_TS.read_text(encoding="utf-8")
    start = source.index("export const FAMILY_CATALOG_COPY")
    body = source[start : source.index("] as const", start)]
    listed = {json.loads(m.group(0)) for m in re.finditer(r'"(?:[^"\\]|\\.)*"', body)}
    served = {text for f in list_families() for text in (f["label"], f["description"])}
    assert served <= listed, (
        f"FAMILY_CATALOG_COPY in {FAMILIES_TS} misses the server's {sorted(served - listed)}; "
        "add them there and to the four catalogs"
    )

# ── Units of performance metrics ──
# The unit is read from the package metadata and carried by the catalogue and both
# metric routes, so the run page never guesses a unit from a metric's key.


@pytest.fixture
def fresh_units():
    registry.reset_cache()
    yield
    registry.reset_cache()


def test_every_performance_metric_has_a_declared_unit(fresh_units) -> None:
    """Every metric the performance headline can use is formatted by its unit."""
    declared = units.performance_units()
    assert set(performance_directions()) <= set(declared)
    assert set(declared.values()) <= _UNITS


@pytest.mark.parametrize(
    ("key", "unit"),
    [
        ("performance.latency.wall_clock", "milliseconds"),
        ("performance.reliability.completion_rate", "ratio"),
        ("performance.cost.llm", "currency"),
        ("performance.tokens.total", "tokens"),
        ("performance.counts.tool_calls", "count"),
        ("performance.latency.ttft", "milliseconds"),
        ("performance.counts.model_calls", "count"),
        ("performance.counts.retries", "count"),
        ("performance.reliability.tool_error_rate", "ratio"),
    ],
)
def test_a_metric_resolves_to_the_unit_the_package_declares(fresh_units, key, unit) -> None:
    assert units.metric_unit(key) == unit


def test_quality_platform_and_unknown_keys_have_no_unit(fresh_units) -> None:
    """A quality score is 0-100, and nothing else is declared: no unit is invented."""
    assert all(units.metric_unit(key) is None for key in quality_directions())
    assert units.metric_unit("judge_seconds") is None
    assert units.metric_unit("performance.not_a_metric") is None


def test_a_key_declared_with_two_units_is_left_unformatted(fresh_units, monkeypatch) -> None:
    """Either unit could be wrong by a factor of a thousand, so neither is reported."""
    from app.evaluation_metrics.core.contracts import MetricDirection
    from app.evaluation_metrics.performance.results import PerformanceMetadata
    from app.evaluation_metrics.performance.units import MetricUnit

    def meta(key: str, unit: MetricUnit) -> PerformanceMetadata:
        return PerformanceMetadata(
            key=key, label=key, kind="count", primary_measure="value",
            unit=unit, direction=MetricDirection.lower_is_better, rollup="mean",
        )

    performance_root = types.ModuleType("fake_performance_root")
    performance_root.__path__ = []  # type: ignore[attr-defined]
    performance_root._A1 = meta("performance.a", MetricUnit.milliseconds)
    performance_root._A2 = meta("performance.a", MetricUnit.milliseconds)
    performance_root._B1 = meta("performance.b", MetricUnit.milliseconds)
    performance_root._B2 = meta("performance.b", MetricUnit.tokens)
    quality_root = types.ModuleType("fake_quality_root")
    quality_root.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setattr(
        registry, "_collect", lambda: registry.build_registry(quality_root, performance_root)
    )
    registry.reset_cache()

    assert units.performance_units() == {"performance.a": "milliseconds"}


async def test_both_metric_routes_emit_the_unit(monkeypatch) -> None:
    """The routes build the rows: a field the schema declares but no route sets is lost too."""
    from app.api.v1 import evaluations as routes
    from app.services.evaluation import metrics as run_metrics

    run_id = "00000000-0000-0000-0000-000000000001"
    result_id = "00000000-0000-0000-0000-000000000002"

    class _Rows:
        def all(self):
            return [("judge_seconds", 1.2), ("performance.latency.wall_clock", 900.0)]

    class _Db:
        async def execute(self, _statement):
            return _Rows()

    per_result = await routes.get_result_metrics(run_id, result_id, user=None, db=_Db())
    assert {r["key"]: r["unit"] for r in per_result} == {
        "judge_seconds": None,
        "performance.latency.wall_clock": "milliseconds",
    }

    async def load_run(_db, _run_id):
        return types.SimpleNamespace(id=run_id)

    async def aggregates(_db, _run_id):
        return {"performance.reliability.tool_error_rate": {"mean": 0.1, "count": 2}}

    monkeypatch.setattr(routes, "_load_run_or_404", load_run)
    monkeypatch.setattr(run_metrics, "get_run_aggregates", aggregates)

    aggregate = await routes.get_run_metrics_aggregate(run_id, user=None, db=_Db())
    assert aggregate["aggregates"][0]["unit"] == "ratio"
