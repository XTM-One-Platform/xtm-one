"""The STIX_BUNDLE metric family: the optional OpenCTI canonicalizer and
standards validator backends, the shared envelope check, entity and relationship P/R/F1,
the semantic-validity gate, envelope validity, and the golden fixtures.
"""

import importlib
import socket
import sys
from collections.abc import Callable, Mapping

import pytest
import requests
from pydantic import ValidationError

from app.evaluation_metrics.core.contracts import MetricDirection, MetricError, MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.backends.stix_opencti import OpenCtiCanonicalizer
from app.evaluation_metrics.quality.backends.stix_validator import StandardsStixValidator
from app.evaluation_metrics.quality.families.stix_bundle import (
    Canonicalizer,
    StixValidator,
    stix_entity_scores,
    stix_semantic_validity,
)
from app.evaluation_metrics.quality.families.stix_bundle.canonical import StixObjectTypeSelection
from app.evaluation_metrics.quality.families.stix_bundle.envelope import BundleStructureError, extract_bundle_objects
from app.evaluation_metrics.quality.families.stix_bundle.metrics import stix_relationship_scores
from app.evaluation_metrics.quality.families.stix_bundle.validator import StixValidationReport
from app.evaluation_metrics.quality.families.stix_bundle.validity import stix_envelope_validity

# ── OpenCTI canonicalizer backend (pycti, stix2) ──
# pycti supplies identifiers for the STIX types it supports, SCOs get OASIS stix2
# deterministic identifiers, and there is no raw-ID fallback, no defang normalization and no
# network access. Also the checks shared by both optional backends: each implements its
# protocol, and a missing dependency fails at import with an actionable error.

_RANDOM_IPV4_ID = "ipv4-addr--11111111-1111-4111-8111-111111111111"
_RANDOM_MALWARE_ID = "malware--22222222-2222-4222-8222-222222222222"


def _bundle() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-4000-8000-000000000001",
        "objects": [
            {"type": "malware", "id": _RANDOM_MALWARE_ID, "name": "Emotet"},
            {"type": "ipv4-addr", "id": _RANDOM_IPV4_ID, "value": "10.0.0.1"},
            {
                "type": "relationship",
                "id": "relationship--33333333-3333-4333-8333-333333333333",
                "relationship_type": "communicates-with",
                "source_ref": _RANDOM_MALWARE_ID,
                "target_ref": _RANDOM_IPV4_ID,
            },
        ],
    }


def _objects(canonical: object) -> list[dict[str, object]]:
    assert isinstance(canonical, dict)
    objects = canonical["objects"]
    assert isinstance(objects, list)
    return objects


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on any outbound HTTP or socket connection."""

    def _no_http(*args: object, **kwargs: object) -> object:
        raise AssertionError("network access attempted via requests")

    def _no_socket(*args: object, **kwargs: object) -> object:
        raise AssertionError("network access attempted via socket.create_connection")

    monkeypatch.setattr(requests.sessions.Session, "request", _no_http)
    monkeypatch.setattr(socket, "create_connection", _no_socket)


@pytest.mark.parametrize(
    ("backend", "protocol"),
    [
        pytest.param(OpenCtiCanonicalizer, Canonicalizer, id="opencti-canonicalizer"),
        pytest.param(StandardsStixValidator, StixValidator, id="standards-validator"),
    ],
)
def test_each_backend_implements_its_protocol(backend: type, protocol: type) -> None:
    assert isinstance(backend(), protocol)


def test_sdo_id_comes_from_pycti(offline: None) -> None:
    result = _objects(OpenCtiCanonicalizer().canonicalize(_bundle()))
    malware = next(o for o in result if o["type"] == "malware")
    assert malware["id"] != _RANDOM_MALWARE_ID
    assert str(malware["id"]).startswith("malware--")


def test_sco_id_uses_oasis_deterministic_generation(offline: None) -> None:
    result = _objects(OpenCtiCanonicalizer().canonicalize(_bundle()))
    ipv4 = next(o for o in result if o["type"] == "ipv4-addr")
    # OASIS STIX 2.1 deterministic UUIDv5 for ipv4-addr value 10.0.0.1.
    assert ipv4["id"] == "ipv4-addr--7dd44d27-f473-5ba9-b12b-0d3a61bbed2e"


def test_same_value_different_raw_id_canonicalizes_identically(offline: None) -> None:
    other = _bundle()
    objects = _objects(other)
    objects[1]["id"] = "ipv4-addr--99999999-9999-4999-8999-999999999999"
    a = _objects(OpenCtiCanonicalizer().canonicalize(_bundle()))
    b = _objects(OpenCtiCanonicalizer().canonicalize(other))
    assert next(o for o in a if o["type"] == "ipv4-addr")["id"] == next(
        o for o in b if o["type"] == "ipv4-addr"
    )["id"]


def test_relationship_references_are_rewritten(offline: None) -> None:
    result = _objects(OpenCtiCanonicalizer().canonicalize(_bundle()))
    ids = {str(o["id"]) for o in result if o["type"] != "relationship"}
    relationship = next(o for o in result if o["type"] == "relationship")
    assert relationship["source_ref"] in ids
    assert relationship["target_ref"] in ids
    assert relationship["source_ref"] != _RANDOM_MALWARE_ID
    assert relationship["target_ref"] != _RANDOM_IPV4_ID


def test_ref_list_properties_are_rewritten(offline: None) -> None:
    bundle = _bundle()
    _objects(bundle).append(
        {
            "type": "report",
            "id": "report--44444444-4444-4444-8444-444444444444",
            "name": "R",
            "published": "2020-01-01T00:00:00Z",
            "object_refs": [_RANDOM_MALWARE_ID, _RANDOM_IPV4_ID],
        }
    )
    result = _objects(OpenCtiCanonicalizer().canonicalize(bundle))
    report = next(o for o in result if o["type"] == "report")
    refs = report["object_refs"]
    assert isinstance(refs, list)
    assert _RANDOM_MALWARE_ID not in refs
    assert _RANDOM_IPV4_ID not in refs


def test_an_unknown_type_is_identified_by_its_content(offline: None) -> None:
    """A type neither implementation knows gets an identifier from what it says:
    the same content matches whoever wrote it and whatever id it refers to the same object
    by; other content does not."""

    def canonical_id(thing: dict, *, malware_id: str = _RANDOM_MALWARE_ID) -> str:
        bundle = _bundle()
        _objects(bundle)[0]["id"] = malware_id
        _objects(bundle).append({"type": "x-custom-thing", "object_refs": [malware_id], **thing})
        return _objects(OpenCtiCanonicalizer().canonicalize(bundle))[-1]["id"]

    first = canonical_id({"id": "x-custom-thing--1", "name": "a", "created": "2026-01-01T00:00:00Z"})
    assert first.startswith("x-custom-thing--")
    assert canonical_id(
        {"id": "x-custom-thing--2", "name": "a", "created": "2026-02-02T00:00:00Z"},
        malware_id="malware--00000000-0000-4000-8000-000000000999",
    ) == first
    assert canonical_id({"id": "x-custom-thing--1", "name": "b"}) != first


def test_no_defang_normalization_is_applied(offline: None) -> None:
    """A defanged value stays defanged and keeps its own identifier."""
    bundle = _bundle()
    _objects(bundle)[1]["value"] = "10.0.0[.]1"
    result = _objects(OpenCtiCanonicalizer().canonicalize(bundle))
    ipv4 = next(o for o in result if o["type"] == "ipv4-addr")
    assert ipv4["value"] == "10.0.0[.]1"
    assert ipv4["id"] != "ipv4-addr--7dd44d27-f473-5ba9-b12b-0d3a61bbed2e"


def test_input_bundle_is_not_mutated(offline: None) -> None:
    bundle = _bundle()
    OpenCtiCanonicalizer().canonicalize(bundle)
    assert _objects(bundle)[0]["id"] == _RANDOM_MALWARE_ID


def test_non_bundle_input_is_returned_unchanged(offline: None) -> None:
    canonicalizer = OpenCtiCanonicalizer()
    assert canonicalizer.canonicalize(["not", "a", "bundle"]) == ["not", "a", "bundle"]


def test_scores_perfectly_against_itself_through_the_metric(offline: None) -> None:
    canonicalizer = OpenCtiCanonicalizer()
    results = stix_entity_scores(_bundle(), _bundle(), canonicalizer)
    entity = results["stix.entity_prf"]
    assert entity.status is MetricStatus.scored
    assert entity.value == 100.0


@pytest.mark.parametrize(
    ("module", "dependency"),
    [
        pytest.param("app.evaluation_metrics.quality.backends.stix_opencti", "pycti", id="pycti"),
        pytest.param(
            "app.evaluation_metrics.quality.backends.stix_validator", "stix2", id="stix2"
        ),
    ],
)
def test_import_without_the_dependency_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch, module: str, dependency: str
) -> None:
    """A missing optional dependency fails at import with a clear, actionable ImportError.

    The suite ships inside the backend, so the remedy is the backend's own dependency
    list (``pyproject.toml``), not an extra of a separate distribution.
    """
    monkeypatch.setitem(sys.modules, dependency, None)
    monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(ImportError) as excinfo:
        importlib.import_module(module)
    message = str(excinfo.value)
    assert dependency in message
    assert "pyproject.toml" in message


# ── standards validator backend (stix2) ──
# Complete STIX 2.1 semantic validation via `stix2.parse(..., allow_custom=True,
# version="2.1")`, structured `invalid` reports on parsing failures, no network access.

def _valid_bundle() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-4000-8000-000000000001",
        "objects": [
            {
                "type": "malware",
                "spec_version": "2.1",
                "id": "malware--22222222-2222-4222-8222-222222222222",
                "created": "2020-01-01T00:00:00.000Z",
                "modified": "2020-01-01T00:00:00.000Z",
                "name": "Emotet",
                "is_family": False,
            }
        ],
    }


def test_valid_bundle_reports_valid(offline: None) -> None:
    report = StandardsStixValidator().validate(_valid_bundle())
    assert report.valid is True
    assert report.findings == []


def test_missing_required_property_is_invalid(offline: None) -> None:
    bundle = _valid_bundle()
    objects = bundle["objects"]
    assert isinstance(objects, list)
    del objects[0]["is_family"]  # required for malware in STIX 2.1
    report = StandardsStixValidator().validate(bundle)
    assert report.valid is False
    assert report.findings
    assert report.findings[0].code == "stix_parse_error"
    assert report.findings[0].message


def test_malformed_identifier_is_invalid(offline: None) -> None:
    bundle = _valid_bundle()
    objects = bundle["objects"]
    assert isinstance(objects, list)
    objects[0]["id"] = "not-a-stix-id"
    report = StandardsStixValidator().validate(bundle)
    assert report.valid is False
    assert report.findings


@pytest.mark.parametrize("prop", ["modified", "created", "spec_version", "id"])
def test_a_required_property_stix2_would_fill_in_is_reported_missing(offline: None, prop: str) -> None:
    """stix2 defaults id, created and modified: the bundle as written is checked first."""
    bundle = _valid_bundle()
    objects = bundle["objects"]
    assert isinstance(objects, list)
    del objects[0][prop]
    report = StandardsStixValidator().validate(bundle)
    assert report.valid is False
    missing = [finding for finding in report.findings if finding.code == "missing_required_property"]
    assert [list(finding.locations) for finding in missing] == [[f"objects[0].{prop}"]]
    assert repr(prop) in missing[0].message
    result = stix_semantic_validity(bundle, StandardsStixValidator())
    assert (result.status, result.value) == (MetricStatus.scored, 0.0)


def test_a_modified_earlier_than_created_is_a_finding(offline: None) -> None:
    bundle = _valid_bundle()
    objects = bundle["objects"]
    assert isinstance(objects, list)
    objects[0]["modified"] = "2019-01-01T00:00:00.000Z"
    report = StandardsStixValidator().validate(bundle)
    assert report.valid is False
    assert [finding.code for finding in report.findings] == ["modified_before_created"]


def test_a_marking_definition_needs_no_modified(offline: None) -> None:
    bundle = _valid_bundle()
    objects = bundle["objects"]
    assert isinstance(objects, list)
    objects.append(
        {
            "type": "marking-definition",
            "spec_version": "2.1",
            "id": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
            "created": "2017-01-20T00:00:00.000Z",
            "definition_type": "tlp",
            "name": "TLP:WHITE",
            "definition": {"tlp": "white"},
        }
    )
    assert StandardsStixValidator().validate(bundle).findings == []


def test_non_bundle_input_is_invalid_not_an_exception(offline: None) -> None:
    report = StandardsStixValidator().validate(["not", "a", "bundle"])
    assert report.valid is False
    assert report.findings
    assert report.findings[0].code == "stix_parse_error"


def test_metric_scores_through_the_validator(offline: None) -> None:
    result = stix_semantic_validity(_valid_bundle(), StandardsStixValidator())
    assert result.status is MetricStatus.scored
    assert result.value == 100.0


def test_metric_reports_scored_zero_through_the_validator(offline: None) -> None:
    # An invalid bundle scores 0, not `invalid`, which the run aggregate would
    # silently drop.
    bundle = _valid_bundle()
    objects = bundle["objects"]
    assert isinstance(objects, list)
    objects[0]["id"] = "not-a-stix-id"
    result = stix_semantic_validity(bundle, StandardsStixValidator())
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    assert result.error is None
    assert result.detail["findings"]


# ── shared bundle-envelope check ──
# One source of truth: a bundle rejected by `stix.envelope_validity` can never be scored by
# the entity/relationship metrics.

class _IdentityCanonicalizer:
    """Returns the bundle unchanged (ids already canonical)."""

    def canonicalize(self, bundle: object) -> object:
        return bundle


def _valid_bundle_envelope() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--1",
        "objects": [{"type": "indicator", "id": "indicator--a1"}],
    }


# Values that are structurally not a STIX bundle envelope, with the location they fail at.
_MALFORMED: list[tuple[str, object, tuple[str, ...]]] = [
    ("not_a_mapping", ["not", "a", "bundle"], ()),
    ("missing_type", {"id": "bundle--1", "objects": []}, ("/type",)),
    ("wrong_type", {"type": "indicator", "id": "bundle--1", "objects": []}, ("/type",)),
    ("objects_missing", {"type": "bundle", "id": "bundle--1"}, ("/objects",)),
    ("objects_not_a_list", {"type": "bundle", "id": "bundle--1", "objects": {}}, ("/objects",)),
    (
        "object_not_a_mapping",
        {"type": "bundle", "id": "bundle--1", "objects": ["x"]},
        ("/objects/0",),
    ),
    (
        "object_missing_type",
        {"type": "bundle", "id": "bundle--1", "objects": [{"id": "indicator--a1"}]},
        ("/objects/0/type",),
    ),
    (
        "object_missing_id",
        {"type": "bundle", "id": "bundle--1", "objects": [{"type": "indicator"}]},
        ("/objects/0/id",),
    ),
    (
        "object_non_string_id",
        {
            "type": "bundle",
            "id": "bundle--1",
            "objects": [{"type": "indicator", "id": 7}],
        },
        ("/objects/0/id",),
    ),
]


@pytest.mark.parametrize(
    ("bundle", "locations"),
    [(bundle, locations) for _name, bundle, locations in _MALFORMED],
    ids=[name for name, _bundle, _locations in _MALFORMED],
)
def test_malformed_rejected_by_envelope_and_by_metrics(
    bundle: object, locations: tuple[str, ...]
) -> None:
    """Envelope validity and the scoring metrics agree on what is malformed, and where.

    A malformed prediction scores 0 on every path, not `invalid`, which the run aggregate
    would silently drop.
    """
    entity = stix_entity_scores(_valid_bundle_envelope(), bundle, _IdentityCanonicalizer())
    relationship = stix_relationship_scores(_valid_bundle_envelope(), bundle, _IdentityCanonicalizer())
    for result in (
        stix_envelope_validity(bundle),
        entity["stix.entity_prf"],
        entity["stix.entity_prf.by_type"],
        relationship,
    ):
        assert result.status is MetricStatus.scored, result.metadata.key
        assert result.value == 0.0, result.metadata.key
        assert result.error is None, result.metadata.key
        assert result.detail["error_code"] == "stix_invalid", result.metadata.key
        assert result.detail["locations"] == locations, result.metadata.key

    # And as a reference it is caller misconfiguration, never a score.
    with pytest.raises(ConfigurationError):
        stix_entity_scores(bundle, _valid_bundle_envelope(), _IdentityCanonicalizer())
    with pytest.raises(ConfigurationError):
        stix_relationship_scores(bundle, _valid_bundle_envelope(), _IdentityCanonicalizer())


def test_extract_returns_typed_objects() -> None:
    objects = extract_bundle_objects(_valid_bundle_envelope())
    assert len(objects) == 1
    assert objects[0].type == "indicator"
    assert objects[0].id == "indicator--a1"
    assert objects[0].location == "/objects/0"


def test_extract_reports_offending_location() -> None:
    bundle = {
        "type": "bundle",
        "id": "bundle--1",
        "objects": [
            {"type": "indicator", "id": "indicator--a1"},
            {"type": "malware"},
        ],
    }
    with pytest.raises(BundleStructureError) as excinfo:
        extract_bundle_objects(bundle)
    assert excinfo.value.locations == ["/objects/1/id"]


# ── entity and relationship P/R/F1 over canonical identifiers ──
# The default entity selection (SROs, sightings and administrative objects excluded),
# per-type breakdowns, relationship triple matching, and a canonicalizer always supplied and
# applied identically to both bundles.

class _LowercaseCanonicalizer:
    """Canonicalizes by lowercasing every id and relationship reference.

    Proves the same canonicalizer/policy is applied to both sides and that raw
    non-canonical IDs are never compared directly.
    """

    def canonicalize(self, bundle: object) -> object:
        assert isinstance(bundle, dict)
        objects = []
        for obj in bundle["objects"]:
            assert isinstance(obj, dict)
            new = dict(obj)
            if isinstance(new.get("id"), str):
                new["id"] = new["id"].lower()
            for ref in ("source_ref", "target_ref"):
                if isinstance(new.get(ref), str):
                    new[ref] = new[ref].lower()
            objects.append(new)
        return {"type": "bundle", "id": bundle.get("id"), "objects": objects}


def _reference() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--r",
        "objects": [
            {"type": "indicator", "id": "indicator--a1"},
            {"type": "malware", "id": "malware--b1"},
            {"type": "ipv4-addr", "id": "ipv4-addr--c1", "value": "10.0.0.1"},
            {"type": "marking-definition", "id": "marking-definition--d1"},
            {"type": "sighting", "id": "sighting--e1", "sighting_of_ref": "indicator--a1"},
            {
                "type": "relationship",
                "id": "relationship--f1",
                "relationship_type": "indicates",
                "source_ref": "indicator--a1",
                "target_ref": "malware--b1",
            },
        ],
    }


def _prediction() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--p",
        "objects": [
            {"type": "indicator", "id": "indicator--a1"},
            {"type": "malware", "id": "malware--b1"},
            {"type": "ipv4-addr", "id": "ipv4-addr--c2", "value": "10.0.0.2"},
            {
                "type": "relationship",
                "id": "relationship--f1",
                "relationship_type": "indicates",
                "source_ref": "indicator--a1",
                "target_ref": "malware--b1",
            },
            {
                "type": "relationship",
                "id": "relationship--f2",
                "relationship_type": "uses",
                "source_ref": "indicator--a1",
                "target_ref": "ipv4-addr--c2",
            },
        ],
    }


@pytest.mark.parametrize(
    "metric", [stix_entity_scores, stix_relationship_scores], ids=["entity", "relationship"]
)
@pytest.mark.parametrize("passed", ["none", "omitted"])
def test_a_missing_canonicalizer_raises(metric: Callable[..., object], passed: str) -> None:
    """The canonicalizer is optional at the type level, so the documented error must be
    reachable when it is omitted entirely, not only when it is passed as None."""
    with pytest.raises(ConfigurationError):
        if passed == "none":
            metric(_reference(), _prediction(), None)
        else:
            metric(_reference(), _prediction())


def test_entity_scores_default_selection() -> None:
    results = stix_entity_scores(
        _reference(), _prediction(), _IdentityCanonicalizer()
    )
    entity = results["stix.entity_prf"]
    assert entity.status is MetricStatus.scored
    assert entity.value == pytest.approx(200 / 3)
    assert entity.detail["tp"] == 2
    assert entity.detail["fp"] == 1
    assert entity.detail["fn"] == 1
    assert entity.detail["missing"] == ("ipv4-addr--c1",)
    assert entity.detail["spurious"] == ("ipv4-addr--c2",)


def test_entity_by_type_macro_breakdown() -> None:
    results = stix_entity_scores(
        _reference(), _prediction(), _IdentityCanonicalizer()
    )
    by_type = results["stix.entity_prf.by_type"]
    assert by_type.metadata.key == "stix.entity_prf.by_type"
    assert by_type.status is MetricStatus.scored
    per_type = by_type.detail["classes"]
    assert isinstance(per_type, Mapping)
    assert set(per_type) == {"indicator", "malware", "ipv4-addr"}
    # macro over indicator(1.0), malware(1.0), ipv4-addr(0.0) -> 2/3
    assert by_type.value == pytest.approx(200 / 3)


def test_relationship_scores_over_canonical_triples() -> None:
    result = stix_relationship_scores(
        _reference(), _prediction(), _IdentityCanonicalizer()
    )
    assert result.status is MetricStatus.scored
    assert result.metadata.key == "stix.relationship_prf"
    # The reference's sighting, which names no place, is a triple too.
    assert result.value == pytest.approx(50.0)
    assert (result.detail["tp"], result.detail["fp"], result.detail["fn"]) == (1, 1, 1)
    assert result.detail["spurious"] == (
        ("indicator--a1", "uses", "ipv4-addr--c2"),
    )
    assert result.detail["missing"] == (("indicator--a1", "sighting", ""),)


def test_same_canonicalizer_applied_to_both_sides() -> None:
    # Reference uses upper-case raw ids; prediction uses lower-case. A raw comparison
    # would score 0, but the shared canonicalizer lower-cases both, yielding a match.
    reference: dict[str, object] = {
        "type": "bundle",
        "id": "bundle--r",
        "objects": [{"type": "indicator", "id": "INDICATOR--A1"}],
    }
    prediction: dict[str, object] = {
        "type": "bundle",
        "id": "bundle--p",
        "objects": [{"type": "indicator", "id": "indicator--a1"}],
    }
    results = stix_entity_scores(
        reference, prediction, _LowercaseCanonicalizer()
    )
    entity = results["stix.entity_prf"]
    assert entity.value == 100.0
    assert entity.detail["tp"] == 1


@pytest.mark.parametrize(
    ("relationship", "locations"),
    [
        pytest.param(
            {"type": "relationship", "id": "relationship--f1"},
            ("/objects/0/source_ref",),
            id="missing-refs",
        ),
        pytest.param(
            {
                "type": "relationship",
                "id": "relationship--f1",
                "relationship_type": "indicates",
                "source_ref": "indicator--a1",
                "target_ref": 42,
            },
            ("/objects/0/target_ref",),
            id="non-string-target-ref",
        ),
    ],
)
def test_a_malformed_relationship_scores_zero_at_its_field_location(
    relationship: dict[str, object], locations: tuple[str, ...]
) -> None:
    """The agent's own malformed relationship counts as a real 0, not a dropped result,
    and names the offending field."""
    prediction: dict[str, object] = {
        "type": "bundle",
        "id": "bundle--p",
        "objects": [relationship],
    }
    result = stix_relationship_scores(_reference(), prediction, _IdentityCanonicalizer())
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    assert result.error is None
    assert result.detail["error_code"] == "stix_invalid"
    assert result.detail["locations"] == locations


def test_selection_allowlist_override() -> None:
    selection = StixObjectTypeSelection(allowlist=frozenset({"indicator"}))
    results = stix_entity_scores(
        _reference(), _prediction(), _IdentityCanonicalizer(), selection
    )
    entity = results["stix.entity_prf"]
    # Only indicator entities are considered -> a single shared TP, no FP/FN.
    assert entity.detail["tp"] == 1
    assert entity.detail["fp"] == 0
    assert entity.detail["fn"] == 0
    assert entity.value == 100.0


def test_selection_excludelist_override() -> None:
    # Exclude ipv4-addr in addition to defaults; only the two SDOs remain.
    selection = StixObjectTypeSelection(
        excludelist=frozenset(
            {
                "relationship",
                "sighting",
                "marking-definition",
                "language-content",
                "extension-definition",
                "ipv4-addr",
            }
        )
    )
    results = stix_entity_scores(
        _reference(), _prediction(), _IdentityCanonicalizer(), selection
    )
    entity = results["stix.entity_prf"]
    assert entity.detail["tp"] == 2
    assert entity.detail["fp"] == 0
    assert entity.detail["fn"] == 0
    assert entity.value == 100.0


def test_malformed_reference_relationship_raises_configuration_error() -> None:
    # A relationship missing 'target_ref' in the ground truth is caller misconfiguration.
    reference: dict[str, object] = {
        "type": "bundle",
        "id": "bundle--r",
        "objects": [
            {
                "type": "relationship",
                "id": "relationship--f1",
                "relationship_type": "indicates",
                "source_ref": "indicator--a1",
            }
        ],
    }
    with pytest.raises(ConfigurationError):
        stix_relationship_scores(reference, _prediction(), _IdentityCanonicalizer())


def test_selection_allowlist_applied_before_exclusions() -> None:
    # 'indicator' is allowlisted but also excluded -> exclusions are not discarded.
    selection = StixObjectTypeSelection(
        allowlist=frozenset({"indicator", "malware"}),
        excludelist=frozenset({"indicator"}),
    )
    assert selection.includes("malware") is True
    assert selection.includes("indicator") is False
    # A type in neither list is still outside the allowlist.
    assert selection.includes("ipv4-addr") is False

    results = stix_entity_scores(
        _reference(), _prediction(), _IdentityCanonicalizer(), selection
    )
    entity = results["stix.entity_prf"]
    # Only malware survives: one shared TP, nothing else scored.
    assert entity.detail["tp"] == 1
    assert entity.detail["fp"] == 0
    assert entity.detail["fn"] == 0


def test_default_selection_includes_sdo_sco_and_excludes_sro_and_meta() -> None:
    selection = StixObjectTypeSelection()
    for included in ("indicator", "malware", "ipv4-addr", "file", "threat-actor"):
        assert selection.includes(included) is True
    for excluded in (
        "relationship",
        "sighting",
        "marking-definition",
        "language-content",
        "extension-definition",
    ):
        assert selection.includes(excluded) is False


# `detail` payload shape contract
# ---------------------------------------------------------------------------
# The run-detail UI renders the PRF breakdown by inspecting these payloads
# STRUCTURALLY, never by metric id. A renamed or dropped field makes its type guard
# stop matching and the disclosure silently disappear, so these pin the key set.


def test_flat_prf_detail_shape_is_stable() -> None:
    entity = stix_entity_scores(_reference(), _prediction(), _IdentityCanonicalizer())[
        "stix.entity_prf"
    ]
    relationship = stix_relationship_scores(
        _reference(), _prediction(), _IdentityCanonicalizer()
    )
    for result in (entity, relationship):
        detail = result.detail
        assert isinstance(detail, Mapping)
        for numeric in ("precision", "recall", "f1", "tp", "fp", "fn"):
            assert isinstance(detail[numeric], (int, float)), numeric
        # 0..100, not 0..1: the UI appends a percent sign without rescaling.
        for percentage in ("precision", "recall", "f1"):
            assert 0.0 <= float(detail[percentage]) <= 100.0, percentage
        for sequence in ("missing", "spurious"):
            assert isinstance(detail[sequence], (list, tuple)), sequence
        assert "per_type" not in detail


def test_by_type_prf_detail_shape_is_stable() -> None:
    by_type = stix_entity_scores(_reference(), _prediction(), _IdentityCanonicalizer())[
        "stix.entity_prf.by_type"
    ]
    detail = by_type.detail
    assert isinstance(detail, Mapping)
    for percentage in ("precision", "recall", "f1"):
        assert isinstance(detail[percentage], (int, float)), percentage
    per_type = detail["classes"]
    assert isinstance(per_type, Mapping)
    assert per_type, "empty classes would render an overall score with no rows"
    for stix_type, entry in per_type.items():
        assert isinstance(entry, Mapping), stix_type
        for numeric in ("tp", "fp", "fn", "f1"):
            assert isinstance(entry[numeric], (int, float)), f"{stix_type}.{numeric}"
    # No overall counts, and no id lists: the by-type payload is a different
    # shape, and the UI must not render it through the flat layout.
    for absent in ("tp", "fp", "fn", "missing", "spurious"):
        assert absent not in detail


def test_a_malformed_prediction_scores_zero_and_is_distinguishable_from_prf() -> None:
    """A malformed prediction is a real 0, so the UI cannot filter it out by
    status: `reason` is the only thing separating it from a real breakdown."""
    prediction: dict[str, object] = {"type": "bundle", "id": "bundle--p"}
    results = stix_entity_scores(_reference(), prediction, _IdentityCanonicalizer())
    for key in ("stix.entity_prf", "stix.entity_prf.by_type"):
        assert results[key].status is MetricStatus.scored, key
        assert results[key].value == 0.0, key
        assert results[key].error is None, key
    detail = results["stix.entity_prf"].detail
    assert isinstance(detail, Mapping)
    assert detail["reason"] == "prediction_not_well_formed"
    assert detail["error_code"] == "stix_invalid"
    for absent in ("precision", "recall", "f1", "tp", "fp", "fn", "per_type"):
        assert absent not in detail


# ── StixValidator protocol and the semantic-validity gate ──
# With no backend the metric is `unavailable` with a structured reason, never `invalid`; with
# a backend, findings keep their own code/message/locations and come in a deterministic
# order whatever order the backend emits them in.

class _ValidValidator:
    """A validator backend that reports every bundle as valid."""

    def validate(self, bundle: object) -> StixValidationReport:
        return StixValidationReport(valid=True, findings=[])


class _InvalidValidator:
    """A validator backend that reports a fixed finding."""

    def validate(self, bundle: object) -> StixValidationReport:
        return StixValidationReport(
            valid=False,
            findings=[
                MetricError(
                    code="stix_invalid",
                    message="missing required property 'created'",
                    locations=["/objects/0"],
                ),
            ],
        )


def _bundle_empty() -> dict[str, object]:
    return {"type": "bundle", "id": "bundle--1", "objects": []}


class _MultiFindingValidator:
    """A backend reporting two findings, optionally in a non-deterministic order."""

    def __init__(self, reverse: bool = False) -> None:
        self._reverse = reverse

    def validate(self, bundle: object) -> StixValidationReport:
        findings = [
            MetricError(
                code="stix_invalid",
                message="missing required property 'created'",
                locations=["/objects/0"],
            ),
            MetricError(
                code="stix_ref_error",
                message="dangling reference",
                locations=["/objects/1/source_ref", "/objects/1/target_ref"],
            ),
        ]
        if self._reverse:
            findings.reverse()
        return StixValidationReport(valid=False, findings=findings)


def test_semantic_validity_unavailable_without_backend() -> None:
    result = stix_semantic_validity(_bundle_empty(), validator=None)
    assert result.status is MetricStatus.unavailable
    assert result.value is None
    assert result.detail["reason"] == "no_validator_backend"
    assert result.metadata.key == "stix.semantic_validity"


def test_semantic_validity_scored_with_valid_backend() -> None:
    result = stix_semantic_validity(_bundle_empty(), validator=_ValidValidator())
    assert result.status is MetricStatus.scored
    assert result.value == 100.0


def test_semantic_validity_scores_zero_with_findings_not_invalid() -> None:
    # A failed semantic validation counts as a real 0 in the run aggregate, which
    # `invalid` would drop.
    result = stix_semantic_validity(_bundle_empty(), validator=_InvalidValidator())
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    assert result.error is None
    assert result.detail["error_code"] == "stix_invalid"
    assert result.detail["locations"] == ("/objects/0",)


def test_findings_retain_their_own_code_message_and_locations() -> None:
    result = stix_semantic_validity(_bundle_empty(), validator=_MultiFindingValidator())
    assert result.status is MetricStatus.scored
    assert result.value == 0.0
    findings = result.detail["findings"]
    assert isinstance(findings, tuple)
    # Each finding keeps its own association rather than being flattened into messages.
    assert [dict(finding) for finding in findings] == [
        {
            "code": "stix_invalid",
            "message": "missing required property 'created'",
            "locations": ("/objects/0",),
        },
        {
            "code": "stix_ref_error",
            "message": "dangling reference",
            "locations": ("/objects/1/source_ref", "/objects/1/target_ref"),
        },
    ]


def test_findings_and_locations_are_deterministically_ordered() -> None:
    # The backend emits findings in the opposite order; the result must not change.
    forward = stix_semantic_validity(_bundle_empty(), validator=_MultiFindingValidator())
    reversed_ = stix_semantic_validity(
        _bundle_empty(), validator=_MultiFindingValidator(reverse=True)
    )
    assert forward.to_json() == reversed_.to_json()
    assert forward.detail["locations"] == (
        "/objects/0",
        "/objects/1/source_ref",
        "/objects/1/target_ref",
    )


def test_report_is_frozen() -> None:
    """A report cannot be edited after validation; the mutation itself is attempted."""
    report = StixValidationReport(valid=True, findings=[])

    with pytest.raises(ValidationError):
        report.valid = False
    with pytest.raises(ValidationError):
        report.findings = [MetricError(code="x", message="y")]

    assert report.valid is True
    assert report.findings == []


# ── envelope validity: shallow, never presented as complete STIX 2.1 validity ──

def _bundle_indicator() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-4000-8000-000000000001",
        "objects": [
            {"type": "indicator", "id": "indicator--1"},
        ],
    }


def test_valid_envelope() -> None:
    result = stix_envelope_validity(_bundle_indicator())
    assert result.status is MetricStatus.scored
    assert result.value == 100.0
    assert result.metadata.key == "stix.envelope_validity"
    assert result.metadata.direction is MetricDirection.higher_is_better


def test_envelope_metadata_is_not_complete_validity() -> None:
    # The shallow envelope check must never be presented as complete STIX validity.
    result = stix_envelope_validity(_bundle_indicator())
    assert result.metadata.key == "stix.envelope_validity"
    # It names what it checks, a readable bundle, and claims no validity.
    assert "bundle" in result.metadata.label.lower()
    assert "valid" not in result.metadata.label.lower()


# ── golden fixtures ──


def _stix_id(kind: str, tail: str) -> str:
    return f"{kind}--00000000-0000-4000-8000-{tail:0>12}"


# Each call builds a fresh copy, so no test can change another's input.


def _golden_reference() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": _stix_id("bundle", "1"),
        "objects": [
            {"type": "indicator", "id": _stix_id("indicator", "a1"), "name": "ref-indicator"},
            {"type": "malware", "id": _stix_id("malware", "b1"), "name": "ref-malware"},
            {"type": "ipv4-addr", "id": _stix_id("ipv4-addr", "c1"), "value": "10.0.0.1"},
            {"type": "marking-definition", "id": _stix_id("marking-definition", "d1")},
            {
                "type": "sighting",
                "id": _stix_id("sighting", "e1"),
                "sighting_of_ref": _stix_id("indicator", "a1"),
            },
            {
                "type": "relationship",
                "id": _stix_id("relationship", "f1"),
                "relationship_type": "indicates",
                "source_ref": _stix_id("indicator", "a1"),
                "target_ref": _stix_id("malware", "b1"),
            },
        ],
    }


def _golden_prediction() -> dict[str, object]:
    return {
        "type": "bundle",
        "id": _stix_id("bundle", "2"),
        "objects": [
            {"type": "indicator", "id": _stix_id("indicator", "a1"), "name": "pred-indicator"},
            {"type": "malware", "id": _stix_id("malware", "b1"), "name": "pred-malware"},
            {"type": "ipv4-addr", "id": _stix_id("ipv4-addr", "c2"), "value": "10.0.0.2"},
            {"type": "marking-definition", "id": _stix_id("marking-definition", "d1")},
            {
                "type": "sighting",
                "id": _stix_id("sighting", "e2"),
                "sighting_of_ref": _stix_id("indicator", "a1"),
            },
            {
                "type": "relationship",
                "id": _stix_id("relationship", "f1"),
                "relationship_type": "indicates",
                "source_ref": _stix_id("indicator", "a1"),
                "target_ref": _stix_id("malware", "b1"),
            },
            {
                "type": "relationship",
                "id": _stix_id("relationship", "f2"),
                "relationship_type": "uses",
                "source_ref": _stix_id("indicator", "a1"),
                "target_ref": _stix_id("ipv4-addr", "c2"),
            },
        ],
    }


def test_envelope_validity_golden() -> None:
    result = stix_envelope_validity(_golden_prediction())
    assert result.status is MetricStatus.scored
    assert result.value == 100.0


def test_entity_scores_golden() -> None:
    results = stix_entity_scores(_golden_reference(), _golden_prediction(), _IdentityCanonicalizer())
    entity = results["stix.entity_prf"]
    # indicator + malware shared (TP=2); ipv4 differs (FP=1, FN=1).
    assert entity.value == pytest.approx(200 / 3)
    assert entity.detail["tp"] == 2


def test_relationship_scores_golden() -> None:
    result = stix_relationship_scores(
        _golden_reference(), _golden_prediction(), _IdentityCanonicalizer()
    )
    # indicates triple and the sighting of the indicator shared (TP=2); uses
    # triple spurious (FP=1).
    assert result.value == pytest.approx(80.0)
    assert result.detail["tp"] == 2
    assert result.detail["fp"] == 1
