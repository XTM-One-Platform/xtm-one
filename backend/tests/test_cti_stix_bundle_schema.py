"""Contract tests for the shared STIX 2.1 bundle output schema.

``seed_data/agent_output_schema/cti-stix-harvester.json`` is the single source
of truth for two shipped agents: ``cti-stix-harvester`` (by slug) and
``cti-stix-transformer`` (via ``cti_stix_transformer_schema()``, which only
relaxes ``objects.minItems`` to 0).

The vendored OASIS schema gated custom objects on
``oneOf: [core, cyber-observable-core]``. The two are not disjoint —
``cyber-observable-core`` requires only ``type`` and ``id``, so every
well-formed custom STIX Domain Object satisfied it *as well as* ``core``,
matched both branches, and was rejected. The net effect was that no custom SDO
could ever validate: OpenCTI's FIMI types (``narrative``, ``channel``,
``event``) and the ``x-opencti-*`` containers were all silently invalid, and
``media-content`` slipped through only because it carries no
``created`` / ``modified`` and so matched ``cyber-observable-core`` alone.

That was a bug rather than a policy: the ``stix_bundle_validator`` builtin tool
bound to both agents parses with ``allow_custom=True``, so the runtime layer
already accepted what the schema refused, and the harvester's
``finalize_on_builtin_tool`` short-circuit returns a bundle the tool blessed.
The branch now uses ``anyOf``, which widens it by exactly the both-match case.

These tests pin both halves of that contract — the custom SDOs that must now
validate, and the malformed objects that must still be rejected, so a re-vendor
from OASIS (which reintroduces ``oneOf``) or an over-broad "fix" that guts the
per-type constraints both fail at CI time rather than in production.
"""

from __future__ import annotations

import jsonschema
import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.output_schema import cti_stix_transformer_schema
from app.agents.seed import _resolve_seed_schema

_HARVESTER_SLUG = "cti-stix-harvester"
_TRANSFORMER_SLUG = "cti-stix-transformer"

_STAMPS = {
    "created": "2026-07-30T10:00:00.000Z",
    "modified": "2026-07-30T10:00:00.000Z",
}

# OpenCTI domain objects that are custom from STIX 2.1's point of view. Each
# carries ``created`` / ``modified``, which is precisely what used to make it
# match ``core`` *and* ``cyber-observable-core`` and get rejected.
_CUSTOM_SDOS = [
    {
        "type": "narrative",
        "id": "narrative--44444444-4444-4444-8444-444444444444",
        "spec_version": "2.1",
        "name": "Election fraud claims",
        **_STAMPS,
    },
    {
        "type": "channel",
        "id": "channel--55555555-5555-4555-8555-555555555555",
        "spec_version": "2.1",
        "name": "A broadcast channel",
        **_STAMPS,
    },
    {
        "type": "event",
        "id": "event--66666666-6666-4666-8666-666666666666",
        "spec_version": "2.1",
        "name": "A staged protest",
        **_STAMPS,
    },
    {
        "type": "x-opencti-case-rfi",
        "id": "x-opencti-case-rfi--77777777-7777-4777-8777-777777777777",
        "spec_version": "2.1",
        "name": "Request for information",
        **_STAMPS,
    },
]

# The one custom object that validated even before the fix — it has no
# ``created`` / ``modified``, so it matched ``cyber-observable-core`` alone.
# Pinned so the fix is shown to be a widening, not a rewrite.
_MEDIA_CONTENT = {
    "type": "media-content",
    "id": "media-content--88888888-8888-4888-8888-888888888888",
    "spec_version": "2.1",
    "url": "https://example.test/article",
}

_STANDARD_OBJECTS = [
    {
        "type": "malware",
        "id": "malware--99999999-9999-4999-8999-999999999999",
        "spec_version": "2.1",
        "name": "A malware family",
        "is_family": True,
        **_STAMPS,
    },
    {
        "type": "identity",
        "id": "identity--aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "spec_version": "2.1",
        "name": "An individual",
        "identity_class": "individual",
        **_STAMPS,
    },
    {
        "type": "ipv4-addr",
        "id": "ipv4-addr--bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "value": "198.51.100.4",
    },
]

# Objects that must STILL be rejected. Without these, relaxing the branch to
# ``anyOf`` could be "fixed" further into accepting anything with a ``type``.
_MUST_REJECT = [
    pytest.param(
        {"type": "narrative", "id": "not-a-stix-identifier", **_STAMPS},
        id="custom-sdo-with-a-malformed-id",
    ),
    pytest.param(
        {"type": "narrative", "spec_version": "2.1", **_STAMPS},
        id="custom-sdo-with-no-id",
    ),
    pytest.param(
        {
            "type": "malware",
            "id": "malware--cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "spec_version": "2.1",
            **_STAMPS,
        },
        id="standard-sdo-missing-its-required-properties",
    ),
    pytest.param(
        {
            "type": "report",
            "id": "report--dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "spec_version": "2.1",
            **_STAMPS,
        },
        id="standard-type-cannot-slip-through-the-custom-branch",
    ),
]


def _agent(slug: str) -> dict:
    for agent in BUILTIN_AGENTS:
        if agent.get("slug") == slug:
            return agent
    # Explicit raise (not ``pytest.fail``) so static analysis sees the
    # not-found path as non-returning instead of an implicit ``None`` return.
    raise AssertionError(f"built-in agent {slug!r} not found in BUILTIN_AGENTS")


@pytest.fixture(scope="module")
def harvester_schema() -> dict:
    """The harvester's schema, resolved the way seeding resolves it."""
    schema = _resolve_seed_schema(_agent(_HARVESTER_SLUG)["output_schema"])
    assert schema is not None, (
        f"{_HARVESTER_SLUG} must resolve to an output schema; a missing seed "
        "file resolves to None and silently produces a schema-less agent"
    )
    return schema


@pytest.fixture(scope="module")
def transformer_schema() -> dict:
    schema = _agent(_TRANSFORMER_SLUG)["output_schema"]
    assert isinstance(schema, dict), (
        f"{_TRANSFORMER_SLUG} must carry an inline resolved output schema"
    )
    return schema


@pytest.fixture(scope="module", params=[_HARVESTER_SLUG, _TRANSFORMER_SLUG])
def bundle_schema(request, harvester_schema, transformer_schema) -> dict:
    """Both agents' schemas — every acceptance test runs against each."""
    return (
        harvester_schema
        if request.param == _HARVESTER_SLUG
        else transformer_schema
    )


def _bundle(*objects: dict) -> dict:
    return {
        "type": "bundle",
        "id": "bundle--11111111-1111-4111-8111-111111111111",
        "objects": list(objects),
    }


def _errors(schema: dict, bundle: dict) -> list:
    return sorted(
        jsonschema.Draft202012Validator(schema).iter_errors(bundle),
        key=lambda err: [str(part) for part in err.absolute_path],
    )


class TestSharedSourceOfTruth:
    """The transformer must stay a thin derivation of the harvester schema."""

    def test_transformer_differs_only_in_min_items(
        self, harvester_schema, transformer_schema
    ):
        assert harvester_schema["properties"]["objects"]["minItems"] == 1
        assert transformer_schema["properties"]["objects"]["minItems"] == 0

        relaxed = cti_stix_transformer_schema()
        relaxed["properties"]["objects"]["minItems"] = harvester_schema[
            "properties"
        ]["objects"]["minItems"]
        assert relaxed == harvester_schema, (
            "cti_stix_transformer_schema() must derive from the harvester "
            "schema with only objects.minItems relaxed, so a fix to the "
            "shared JSON reaches both agents"
        )

    def test_custom_branch_is_not_mutually_exclusive(self, bundle_schema):
        """Guard the exact upstream defect, independent of any one object.

        A re-vendor from OASIS restores ``oneOf`` here, which is what made
        every custom SDO unrepresentable.
        """
        branches = bundle_schema["properties"]["objects"]["items"]["anyOf"]
        custom_branch = next(
            branch for branch in branches if "allOf" in branch
        )
        gate = custom_branch["allOf"][0]
        assert "oneOf" not in gate, (
            "the custom-object branch must not gate on 'oneOf': 'core' and "
            "'cyber-observable-core' overlap, so 'oneOf' rejects every "
            "well-formed custom SDO"
        )
        # Compared unordered: ``anyOf`` semantics don't depend on the order of
        # its subschemas, so a re-vendor that only reshuffles the two refs is
        # behaviourally identical and must not fail this guard.
        assert {ref["$ref"] for ref in gate["anyOf"]} == {
            "#/definitions/core",
            "#/definitions/cyber-observable-core",
        }, (
            "the custom-object gate must still be the choice between 'core' "
            "and 'cyber-observable-core' — a different pair of refs is a "
            "different contract than the one these tests pin"
        )


class TestCustomObjectsValidate:
    @pytest.mark.parametrize(
        "obj", _CUSTOM_SDOS, ids=lambda obj: str(obj["type"])
    )
    def test_custom_sdo_is_accepted(self, bundle_schema, obj):
        errors = _errors(bundle_schema, _bundle(obj))
        assert not errors, (
            f"a well-formed custom SDO of type {obj['type']!r} must validate; "
            "rejected with: "
            + "; ".join(err.message[:200] for err in errors)
        )

    def test_media_content_still_validates(self, bundle_schema):
        """The one custom object that passed before must keep passing."""
        assert not _errors(bundle_schema, _bundle(_MEDIA_CONTENT))

    def test_custom_and_standard_objects_mix_in_one_bundle(
        self, bundle_schema
    ):
        bundle = _bundle(*_CUSTOM_SDOS, _MEDIA_CONTENT, *_STANDARD_OBJECTS)
        errors = _errors(bundle_schema, bundle)
        assert not errors, (
            "a realistic mixed bundle must validate; rejected at: "
            + "; ".join(str(list(err.absolute_path)) for err in errors)
        )


class TestStandardObjectsKeepTheirConstraints:
    @pytest.mark.parametrize(
        "obj", _STANDARD_OBJECTS, ids=lambda obj: str(obj["type"])
    )
    def test_standard_object_is_accepted(self, bundle_schema, obj):
        assert not _errors(bundle_schema, _bundle(obj))

    @pytest.mark.parametrize("obj", _MUST_REJECT)
    def test_malformed_object_is_still_rejected(self, bundle_schema, obj):
        assert _errors(bundle_schema, _bundle(obj)), (
            f"an object of type {obj['type']!r} that violates the spec must "
            "still be rejected — relaxing the custom-object branch must not "
            "turn the schema into a rubber stamp"
        )


class TestBundleCardinality:
    def test_harvester_rejects_an_empty_bundle(self, harvester_schema):
        assert _errors(harvester_schema, _bundle())

    def test_transformer_accepts_an_empty_bundle(self, transformer_schema):
        """A transform that legitimately removes every object yields none."""
        assert not _errors(transformer_schema, _bundle())
