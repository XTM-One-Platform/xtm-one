"""The metric package seam: the registry, the metadata walk it reads, the cache lock, and the
fail-closed error when the package is absent or has drifted.
"""

from __future__ import annotations

import threading
import time
import types
from types import MappingProxyType

import pytest

from app.evaluation_metrics.quality.results import MetricMetadata
from app.services.evaluation.metric_package import (
    MetricPackageUnavailableError,
    catalog,
    directions,
    families,
    guard_package_call,
    registry,
    units,
)
from app.services.evaluation.metric_package import directions as directions_module
from app.services.evaluation.metric_package.availability import MetadataNestedTooDeeplyError
from app.services.evaluation.metric_package.registry import MetricDefinition, Registry
from app.services.evaluation.metric_package.traversal import walk_metric_metadata_by_module

# ── Registry: every view reads one walk of the package ──
# The directions, the catalogue and the units share one walk, so no view can miss a metric
# another finds. A quality metric's family is the family whose sub-package declares it,
# read from the module the walk found it in, never from its key.


_FAMILIES = "app.evaluation_metrics.quality.families."


@pytest.fixture
def fresh_registry():
    registry.reset_cache()
    yield
    registry.reset_cache()


def _quality(key: str) -> MetricMetadata:
    return MetricMetadata(
        key=key, label=key, kind="quality", primary_measure="value", direction="higher_is_better", rollup="mean"
    )


def _walk_returning(quality_pairs):
    """A stand-in for the module-aware walk: *quality_pairs* for the quality root, nothing else."""

    def walk(root, metadata_type):
        return iter(quality_pairs) if metadata_type is MetricMetadata else iter(())

    return walk


def test_a_metrics_family_is_the_sub_package_that_declares_it(monkeypatch, fresh_registry) -> None:
    """The key prefix says nothing: a ``prose.*`` key declared in the STIX sub-package is STIX's."""
    shared = _quality("shared.metric")
    pairs = [
        (f"{_FAMILIES}stix_bundle.metrics", _quality("prose.misfiled")),
        (f"{_FAMILIES}stix_bundle.metrics", shared),
        (f"{_FAMILIES}prose.reexport", shared),
        ("app.evaluation_metrics.quality.primitives.prf", _quality("primitive.only")),
    ]
    monkeypatch.setattr(registry, "walk_metric_metadata_by_module", _walk_returning(pairs))
    registry.reset_cache()

    stix = {m["key"] for m in catalog.quality_catalog(families.STIX_BUNDLE)}
    prose = {m["key"] for m in catalog.quality_catalog(families.PROSE)}

    assert stix == {"prose.misfiled", "shared.metric"}
    assert prose == {"shared.metric"}, "a metric declared in two family modules is offered by both"
    # Outside every family sub-package: oriented, but offered by no family.
    assert directions.metric_direction("primitive.only") == "higher_is_better"
    assert all(
        "primitive.only" not in {m["key"] for m in catalog.quality_catalog(f)}
        for f in registry.FAMILY_MODULES
    )


def test_every_view_comes_from_one_walk_per_root(monkeypatch, fresh_registry) -> None:
    calls: list[str] = []
    real_walk = registry.walk_metric_metadata_by_module

    def counting_walk(root, metadata_type):
        calls.append(root.__name__)
        return real_walk(root, metadata_type)

    monkeypatch.setattr(registry, "walk_metric_metadata_by_module", counting_walk)
    registry.reset_cache()

    for family in registry.FAMILY_MODULES:
        catalog.metric_catalog(family)
    directions.quality_directions()
    directions.performance_directions()
    units.performance_units()

    assert sorted(calls) == [
        "app.evaluation_metrics.performance.metrics",
        "app.evaluation_metrics.quality",
    ], f"the package was walked {len(calls)} times: {calls}"


# --- Rules for a metric declared more than once ----------------------------------


def _loose_quality(key: str, direction: str):
    class LooseMetricMetadata(MetricMetadata):
        direction: str  # type: ignore[assignment]

    return LooseMetricMetadata(
        key=key, label=key, kind="quality", primary_measure="value", direction=direction, rollup="mean"
    )


@pytest.mark.parametrize(
    "declarations",
    [
        ("higher_is_better", "lower_is_better"),
        ("lower_is_better", "higher_is_better"),
        ("sideways", "higher_is_better"),
        ("higher_is_better", "sideways"),
    ],
    ids=["opposite", "opposite_reversed", "unoriented_first", "unoriented_last"],
)
def test_declarations_that_disagree_on_a_direction_leave_the_metric_unoriented(
    monkeypatch, fresh_registry, declarations
) -> None:
    """Whatever the order: it composes no headline, and the catalogue shows a null direction."""
    pairs = [(f"{_FAMILIES}stix_bundle.metrics", _loose_quality("fake.split", d)) for d in declarations]
    pairs.append((f"{_FAMILIES}stix_bundle.metrics", _loose_quality("fake.agreed", "lower_is_better")))
    pairs.append((f"{_FAMILIES}stix_bundle.other", _loose_quality("fake.agreed", "lower_is_better")))
    monkeypatch.setattr(registry, "walk_metric_metadata_by_module", _walk_returning(pairs))
    registry.reset_cache()

    assert directions.metric_direction("fake.split") is None
    assert directions.metric_direction("fake.agreed") == "lower_is_better"
    offered = {m["key"]: m["direction"] for m in catalog.quality_catalog(families.STIX_BUNDLE)}
    assert offered == {"fake.split": None, "fake.agreed": "lower_is_better"}


def test_a_key_declared_in_both_halves_keeps_both_definitions(monkeypatch, fresh_registry) -> None:
    """A performance declaration never overwrites a quality one with the same key.

    The package requires a ``performance.`` prefix on performance keys only, which
    already prevents this; the registry does not rely on it.
    """
    from app.evaluation_metrics.core.contracts import MetricDirection
    from app.evaluation_metrics.performance.results import PerformanceMetadata
    from app.evaluation_metrics.performance.units import MetricUnit

    shared_key = "performance.both_halves"
    performance = PerformanceMetadata(
        key=shared_key, label="perf", kind="count", primary_measure="value",
        unit=MetricUnit.count, direction=MetricDirection.lower_is_better, rollup="mean",
    )

    def walk(root, metadata_type):
        if metadata_type is MetricMetadata:
            return iter([(f"{_FAMILIES}prose.metrics", _quality(shared_key))])
        return iter([("fake.performance", performance)])

    monkeypatch.setattr(registry, "walk_metric_metadata_by_module", walk)
    registry.reset_cache()

    assert directions.is_quality_key(shared_key)
    assert directions.is_performance_key(shared_key)
    assert [m["key"] for m in catalog.quality_catalog(families.PROSE)] == [shared_key]
    assert units.metric_unit(shared_key) == "count"


def test_the_views_are_what_a_plain_walk_of_each_half_gives(fresh_registry) -> None:
    """Each registry view equals a plain walk of its half under the documented rules.

    No frozen snapshot: a registry change that alters a view fails here, while adding
    or removing a metric does not.
    """
    import importlib

    from app.evaluation_metrics import quality
    from app.evaluation_metrics.performance import metrics as performance_metrics
    from app.evaluation_metrics.performance.results import PerformanceMetadata
    from app.services.evaluation.metric_package.registry import HIDDEN_QUALITY_KEYS, VALID_DIRECTIONS
    from app.services.evaluation.metric_package.traversal import walk_metric_metadata_by_module

    def plain(root, metadata_type) -> dict[str, list]:
        found: dict[str, list] = {}
        for _module, m in walk_metric_metadata_by_module(root, metadata_type):
            if m.key not in HIDDEN_QUALITY_KEYS:
                found.setdefault(m.key, []).append(m)
        return found

    def agreed(values: list) -> str | None:
        return values[0] if len(set(values)) == 1 else None

    def direction_of(m) -> str:
        return getattr(m.direction, "value", m.direction)

    for half_root, metadata_type, view in (
        (quality, MetricMetadata, directions.quality_directions()),
        (performance_metrics, PerformanceMetadata, directions.performance_directions()),
    ):
        expected = {
            key: d
            for key, ms in plain(half_root, metadata_type).items()
            if (d := agreed([direction_of(m) for m in ms])) in VALID_DIRECTIONS
        }
        assert view == expected

    expected_units = {
        key: u
        for key, ms in plain(performance_metrics, PerformanceMetadata).items()
        if (u := agreed([str(getattr(m.unit, "value", m.unit)) for m in ms])) is not None
    }
    assert units.performance_units() == expected_units

    for family, package in registry.FAMILY_MODULES.items():
        family_metrics = plain(importlib.import_module(f"{_FAMILIES}{package}"), MetricMetadata)
        expected_catalog = [
            (key, ms[-1].label, d if (d := agreed([direction_of(m) for m in ms])) in VALID_DIRECTIONS else None)
            for key, ms in sorted(family_metrics.items())
        ]
        offered = [(m["key"], m["label"], m["direction"]) for m in catalog.quality_catalog(family)]
        assert offered == expected_catalog, family


def test_declarations_that_disagree_on_a_rollup_compose_no_headline(monkeypatch, fresh_registry) -> None:
    """How the run combines a metric decides its run value: a conflict cannot be resolved
    either way, so the metric composes no headline, whatever the order."""
    from app.evaluation_metrics.core.contracts import MetricRollup

    def declared(rollup: MetricRollup) -> MetricMetadata:
        return MetricMetadata(
            key="fake.split_rollup", label="x", kind="quality", primary_measure="v",
            direction="higher_is_better", rollup=rollup,
        )

    module = f"{_FAMILIES}stix_bundle.metrics"
    monkeypatch.setattr(
        registry,
        "walk_metric_metadata_by_module",
        _walk_returning([(module, declared(MetricRollup.mean)), (module, declared(MetricRollup.pooled))]),
    )
    registry.reset_cache()

    assert directions.metric_direction("fake.split_rollup") is None
    assert "fake.split_rollup" not in registry.registry().pooled_keys


# ── Metadata traversal ──
# A fault in the walk silently empties every view, and a missing metric looks like a
# family with no metrics yet. Every metric declared inside a container is also bound as a
# bare constant, so the suite-level tests cannot see a broken container-unpacking branch:
# these tests pin it directly.


def _walk(root, metadata_type, **seams):
    """The metadata the module-aware walk finds under *root*, without the module names."""
    return [c for _module, c in walk_metric_metadata_by_module(root, metadata_type, **seams)]


def _meta(key: str) -> MetricMetadata:
    return MetricMetadata(
        key=key,
        label=key,
        kind="quality",
        primary_measure="score",
        direction="higher_is_better", rollup="mean",
    )


def _module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


def test_metadata_declared_only_inside_a_container_is_still_found() -> None:
    """A metric bound only as a container member is found, in every collection type.

    It has no bare constant to fall back on, so the unpacking branch is all that finds it.
    """
    root = _module(
        "fake_root",
        _TUPLE=(_meta("fake.tuple"),),
        _LIST=[_meta("fake.list")],
        _SET={_meta("fake.set")},
        _FROZEN=frozenset({_meta("fake.frozen")}),
        _MAPPING={"k": _meta("fake.mapping")},
        _NESTED=((_meta("fake.nested"),),),
    )

    assert sorted(m.key for m in _walk(root, MetricMetadata)) == [
        "fake.frozen",
        "fake.list",
        "fake.mapping",
        "fake.nested",
        "fake.set",
        "fake.tuple",
    ]


def test_a_self_referential_container_does_not_recurse_forever() -> None:
    """Module namespaces can hold cycles; the walk must terminate regardless."""
    cyclic: list[object] = [_meta("fake.cyclic")]
    cyclic.append(cyclic)
    root = _module("fake_root", _CYCLIC=cyclic)

    assert [m.key for m in _walk(root, MetricMetadata)] == ["fake.cyclic"]


def test_unrelated_values_are_ignored() -> None:
    """Tuples of non-metadata must not raise, and must not be collected.

    Modules are full of ordinary tuples; the scan walks every bound value, so an
    unpacking branch that assumed its members were metadata would break on import.
    """
    root = _module(
        "fake_root",
        _META=_meta("fake.real"),
        _BUCKETS=(1, 2, 3),
        _NAMES=("a", "b"),
        _WORD="metadata",
        _EMPTY=(),
        _NUMBER=7,
    )

    assert [m.key for m in _walk(root, MetricMetadata)] == ["fake.real"]


def test_an_unimportable_module_is_skipped_and_traced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken module does not abort the walk and leaves a trace at INFO.

    An absent optional backend and a typo in a required module raise the same
    ImportError; at DEBUG the typo would be invisible and a metric would quietly go
    missing. Driven through the ``_walk``/``_import`` seams because patching
    ``importlib`` would break every lazy import in the process.
    """
    import app.services.evaluation.metric_package.traversal as traversal

    broken = _module("fake_pkg", _META=_meta("fake.present"))

    def _one_broken_submodule(_path, _prefix):
        yield types.SimpleNamespace(name="fake_pkg.broken")

    def _raise(name: str) -> types.ModuleType:
        raise ImportError(f"No module named {name!r}")

    with caplog.at_level("INFO", logger=traversal.logger.name):
        found = [
            m.key
            for m in _walk(
                broken,
                MetricMetadata,
                _walk=_one_broken_submodule,
                _import=_raise,
            )
        ]

    assert found == ["fake.present"], "a broken sub-module must not abort the walk"
    assert any(
        "fake_pkg.broken" in record.message and record.levelname == "INFO"
        for record in caplog.records
    ), f"the skipped module was not traced at INFO: {[r.message for r in caplog.records]}"


def test_metadata_nested_past_the_bound_fails_loudly_rather_than_vanishing() -> None:
    """The nesting bound is never a silent truncation: an incomplete catalogue fails closed."""
    from app.services.evaluation.metric_package.traversal import (
        _MAX_NESTING,
        MetadataNestedTooDeeplyError,
    )

    value: object = _meta("fake.buried")
    for _ in range(_MAX_NESTING + 1):
        value = (value,)
    root = _module("fake_root", _BURIED=value)

    with pytest.raises(MetadataNestedTooDeeplyError, match="nested more than"):
        list(_walk(root, MetricMetadata))


def test_metadata_nested_up_to_the_bound_is_still_collected() -> None:
    """The bound is a backstop, not a budget: everything within it is collected."""
    from app.services.evaluation.metric_package.traversal import _MAX_NESTING

    value: object = _meta("fake.deep")
    for _ in range(_MAX_NESTING):
        value = (value,)
    root = _module("fake_root", _DEEP=value)

    assert [m.key for m in _walk(root, MetricMetadata)] == ["fake.deep"]


def test_a_fabricating_mapping_cannot_blow_the_stack() -> None:
    """``_MAX_NESTING`` stops the one cycle the visited-set cannot see.

    ``_seen`` marks containers by ``id()``; a ``Mapping`` whose ``values()`` builds
    a fresh container per call gets a new id every time, so only the depth bound
    stops the recursion.
    """
    from collections.abc import Mapping

    from app.services.evaluation.metric_package.traversal import (
        MetadataNestedTooDeeplyError,
    )

    class Fabricating(Mapping):
        def __iter__(self):
            return iter(("k",))

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key):
            return [Fabricating()]

        def values(self):
            return [[Fabricating()]]

    root = _module("fake_root", _FABRICATING=Fabricating())

    with pytest.raises(MetadataNestedTooDeeplyError):
        list(_walk(root, MetricMetadata))


def test_a_benign_cycle_closing_at_the_bound_is_not_treated_as_too_deep() -> None:
    """A cycle closing at the bound takes the visited-set exit, not the raise.

    The raise aborts the whole catalogue, and `quality_catalog()` feeds the
    selector, so a structure that merely loops back must not trigger it.
    """
    from app.services.evaluation.metric_package.traversal import _MAX_NESTING

    chain: list = [_meta("fake.looped")]
    innermost = chain
    for _ in range(_MAX_NESTING - 1):
        wrapper: list = [innermost]
        innermost = wrapper
    chain.append(innermost)  # close the loop back onto the outermost container
    root = _module("fake_root", _CYCLE=innermost)

    assert [m.key for m in _walk(root, MetricMetadata)] == ["fake.looped"]


# ── Registry cache under concurrency ──
# The registry is walked once and cached behind one lock. Without the lock the populates
# duplicate the package traversal, and a reset landing inside a populate can hand a
# reader `None`.


THREADS = 8
POPULATE_SECONDS = 0.05
_STUB_DIRECTIONS = {"quality.stub": "higher_is_better"}


def _stub_registry() -> Registry:
    """A registry of one quality and one performance metric, as `build_registry` returns one."""
    return Registry(
        definitions=(
            MetricDefinition(
                key="performance.stub",
                label="Perf stub",
                half="performance",
                direction="lower_is_better",
                rollup="mean",
                unit="milliseconds",
                families=frozenset(),
            ),
            MetricDefinition(
                key="quality.stub",
                label="Stub",
                half="quality",
                direction="higher_is_better",
                rollup="mean",
                unit=None,
                families=frozenset({families.STIX_BUNDLE}),
            ),
        ),
        quality_directions=MappingProxyType(dict(_STUB_DIRECTIONS)),
        performance_directions=MappingProxyType({"performance.stub": "lower_is_better"}),
        performance_units=MappingProxyType({"performance.stub": "milliseconds"}),
        pooled_keys=frozenset(),
        macro_keys=frozenset(),
    )


@pytest.fixture
def stub_registry(monkeypatch):
    monkeypatch.setattr(registry, "_collect", _stub_registry)
    registry.reset_cache()
    yield
    registry.reset_cache()


def _hammer(call, threads: int = THREADS) -> list[BaseException]:
    """Release `threads` callers simultaneously and collect anything they raise."""
    barrier = threading.Barrier(threads)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            call()
        except BaseException as exc:  # surfaced to the main thread, never swallowed
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=30)
    assert not any(w.is_alive() for w in workers), "a caller deadlocked"
    return errors


def _race_a_reset_against_a_populate(read, reset, entered_populate) -> list[BaseException]:
    """Run `read` while `reset` fires the moment it is known to be mid-populate."""
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            read()
        except BaseException as exc:  # surfaced to the main thread, never swallowed
            errors.append(exc)

    def resetter() -> None:
        # Reported rather than raised: an exception here would die inside the thread
        # and leave the test passing without the race ever having been set up.
        if not entered_populate.wait(timeout=10):
            errors.append(AssertionError("the reader never entered the populate"))
            return
        reset()

    threads = [threading.Thread(target=reader), threading.Thread(target=resetter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "reset and populate deadlocked"
    return errors


@pytest.mark.parametrize(
    "read",
    [
        directions.quality_directions,
        directions.performance_directions,
        lambda: catalog.quality_catalog(families.STIX_BUNDLE),
        catalog.performance_catalog,
        units.performance_units,
    ],
    ids=["quality_directions", "performance_directions", "quality_catalog", "performance_catalog", "units"],
)
def test_every_view_populates_the_registry_once_under_concurrency(monkeypatch, read) -> None:
    calls: list[int] = []

    def slow_collect() -> Registry:
        calls.append(1)
        time.sleep(POPULATE_SECONDS)
        return _stub_registry()

    monkeypatch.setattr(registry, "_collect", slow_collect)
    registry.reset_cache()
    try:
        errors = _hammer(read)
        assert errors == [], f"concurrent callers raised: {errors}"
        assert len(calls) == 1, (
            f"the package traversal ran {len(calls)} times for {THREADS} concurrent "
            "first-callers; the populate is not serialised"
        )
    finally:
        registry.reset_cache()


def test_reset_cache_never_exposes_a_half_cleared_registry(monkeypatch) -> None:
    """A reset cannot complete while a populate is in progress.

    The populate assigns the cache, then returns it. A `reset_cache()` outside the lock
    can null the cache in that window and hand the reader `None`. The populate blocks
    and records whether the reset already finished: with the lock it has not.
    """
    entered_populate = threading.Event()
    reset_finished = threading.Event()
    reset_was_early: list[bool] = []

    def blocking_collect() -> Registry:
        entered_populate.set()
        time.sleep(POPULATE_SECONDS)
        reset_was_early.append(reset_finished.is_set())
        return _stub_registry()

    def reset() -> None:
        registry.reset_cache()
        reset_finished.set()

    monkeypatch.setattr(registry, "_collect", blocking_collect)
    registry.reset_cache()

    def read_the_registry() -> None:
        assert directions.quality_directions() == _STUB_DIRECTIONS

    try:
        errors = _race_a_reset_against_a_populate(
            read=read_the_registry,
            reset=reset,
            entered_populate=entered_populate,
        )
        assert errors == [], f"a reader observed a half-cleared registry: {errors}"
        assert reset_was_early == [False], "reset_cache() completed inside a populate; the lock is not held"
    finally:
        registry.reset_cache()


# ── Registry views: read-only inside, copies outside ──
# The cached views are shared and immutable, the public accessors hand out copies, and the
# membership helpers read the same registry.


def test_internal_views_are_read_only(stub_registry) -> None:
    """The cached views are shared by every membership probe, so a write must be
    impossible: one stray assignment would corrupt every later lookup, untraceably.
    """
    quality_view = directions._quality_registry()
    performance_view = directions._performance_registry()
    units_view = registry.registry().performance_units

    for view in (quality_view, performance_view, units_view):
        assert isinstance(view, MappingProxyType)
        with pytest.raises(TypeError):
            view["any.key"] = "x"
    assert isinstance(registry.registry().definitions, tuple)

    # A view, not a copy: it must still see the registry's real content.
    assert dict(quality_view) == _STUB_DIRECTIONS


def test_public_accessors_still_return_independent_copies(stub_registry) -> None:
    """Callers may hold and locally mutate what they get back without touching the cache."""
    first = directions.quality_directions()
    assert isinstance(first, dict)
    first["quality.stub"] = "lower_is_better"
    units_copy = units.performance_units()
    units_copy["performance.stub"] = "tokens"

    assert directions.quality_directions() == _STUB_DIRECTIONS
    assert units.performance_units() == {"performance.stub": "milliseconds"}


def test_membership_helpers_match_the_full_registry(stub_registry) -> None:
    assert directions.is_quality_key("quality.stub") is True
    assert directions.is_quality_key("performance.stub") is False
    assert directions.is_quality_key("nonexistent.key") is False

    assert directions.is_performance_key("performance.stub") is True
    assert directions.is_performance_key("quality.stub") is False

    assert directions.metric_direction("quality.stub") == "higher_is_better"
    assert directions.metric_direction("performance.stub") == "lower_is_better"
    assert directions.metric_direction("nonexistent.key") is None


# ── Fail closed when the package is absent or has drifted ──
# A headline computed from an improvised metric set is the dangerous failure mode: it
# renders as a number on the run row, the leader card and the trend chart, indexed to
# 0-100 exactly like a real one, and nothing on those surfaces can falsify it.


@pytest.mark.parametrize(
    ("failure", "fragment"),
    [
        (ImportError("No module named 'evaluation_metrics'"), "could not import from the package"),
        # Drift is detected by the observed call surface, not by a declared version: an
        # in-repo edit changes `__version__` as easily as the surface.
        (AttributeError("module has no attribute 'METRIC_METADATA'"), "no longer provides"),
        # `MetadataNestedTooDeeplyError` subclasses `RecursionError`; unconverted, a caller
        # handling `MetricPackageUnavailableError` would get an unhandled 500.
        (MetadataNestedTooDeeplyError("nested more than 8 levels deep"), "could not collect the package's metadata"),
    ],
    ids=["missing_package", "drifted_call_surface", "metadata_nested_too_deeply"],
)
def test_a_package_failure_raises_one_fail_closed_error(failure, fragment):
    """Every package failure leaves the seam as ``MetricPackageUnavailableError``.

    The message names the package and the operation, and states that no partial metric set
    is substituted.
    """

    def call() -> None:
        raise failure

    with pytest.raises(MetricPackageUnavailableError) as exc:
        guard_package_call("running the quality suite", call)

    message = str(exc.value)
    assert fragment in message
    assert "evaluation_metrics" in message
    assert "running the quality suite" in message
    assert "no partial or improvised metric set" in message
    assert exc.value.__cause__ is failure


def test_no_headline_is_produced_when_the_package_is_unavailable(monkeypatch):
    """The failure propagates out of the direction lookup instead of returning ``{}``.

    An empty registry would read as "every metric lacks a direction", a silent N/A
    indistinguishable from an evaluation with no data.
    """
    registry.reset_cache()

    def boom():
        raise ImportError("No module named 'evaluation_metrics'")

    monkeypatch.setattr(registry, "_collect", boom)

    with pytest.raises(MetricPackageUnavailableError):
        directions_module.performance_directions()

    registry.reset_cache()


def test_a_real_package_resolves_directions_for_both_halves():
    registry.reset_cache()
    quality = directions_module.quality_directions()
    performance = directions_module.performance_directions()

    assert quality, "the package should declare quality metric directions"
    assert performance, "the package should declare performance metric directions"
    # Orientation is mixed across the package (performance holds both), which is why it
    # is never assumed for either half.
    assert set(quality.values()) | set(performance.values()) == {
        "higher_is_better",
        "lower_is_better",
    }


def test_a_hidden_metric_never_composes_a_headline():
    """The package declares the phrase metrics; the platform hides them."""
    from app.services.evaluation.metric_package.registry import HIDDEN_QUALITY_KEYS

    registry.reset_cache()
    assert HIDDEN_QUALITY_KEYS
    for key in HIDDEN_QUALITY_KEYS:
        assert key not in directions_module.quality_directions()
        assert not directions_module.is_quality_key(key)
