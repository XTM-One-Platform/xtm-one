"""The edit root the OpenCTI label and marking tools write through, per kind.

OpenCTI's ``relationAdd`` / ``relationDelete`` load the element restricted to
their ``*Edit`` root's own type (``patchElementWithRefRelationships`` in
``domain/stixObjectOrStixRelationship.ts``): ``stixDomainObjectEdit`` answers
"Element can not be loaded" for an observable or a relationship, which is how
labelling ~25 Domain-Name / IPv4-Addr observables and their ``resolves-to``
relationships in a draft failed while a Note and a Report in the same draft
took the label. Every SDO and SCO goes through ``stixCoreObjectEdit``, a core
relationship through ``stixCoreRelationshipEdit`` and a sighting through
``stixSightingRelationshipEdit`` - the roots OpenCTI's own views use.
"""

import pytest

import app.integrations.providers.opencti.knowledge as knowledge_mod
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti._common import _operation_names
from app.integrations.providers.opencti.knowledge import _ref_edit_root
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"
DRAFT = "draft-1"

# ``parent_types`` as OpenCTI stores them (``schema/schemaUtils.js``
# ``getParentTypes``) for each kind of element the tools are called on.
_OBSERVABLE_PARENTS = ["Basic-Object", "Stix-Object", "Stix-Core-Object", "Stix-Cyber-Observable"]
_CONTAINER_PARENTS = [
    "Basic-Object",
    "Stix-Object",
    "Stix-Core-Object",
    "Stix-Domain-Object",
    "Container",
]
_CORE_RELATIONSHIP_PARENTS = ["basic-relationship", "stix-relationship", "stix-core-relationship"]
_SIGHTING_PARENTS = ["basic-relationship", "stix-relationship"]

TARGETS = {
    "domain-name": ("Domain-Name", _OBSERVABLE_PARENTS, "stixCoreObjectEdit"),
    "ipv4-addr": ("IPv4-Addr", _OBSERVABLE_PARENTS, "stixCoreObjectEdit"),
    "note": ("Note", _CONTAINER_PARENTS, "stixCoreObjectEdit"),
    "report": ("Report", _CONTAINER_PARENTS, "stixCoreObjectEdit"),
    "resolves-to": ("resolves-to", _CORE_RELATIONSHIP_PARENTS, "stixCoreRelationshipEdit"),
    "sighting": ("stix-sighting-relationship", _SIGHTING_PARENTS, "stixSightingRelationshipEdit"),
}

_NOUN = {
    "stixCoreObjectEdit": "entity",
    "stixCoreRelationshipEdit": "relationship",
    "stixSightingRelationshipEdit": "sighting",
}

# (tool, arguments, mutation operation, ref relation, the ``toId`` it sends)
TOOLS = {
    "add_opencti_label": ({"label_name": "triaged"}, "AddLabel", "object-label", "lbl-1"),
    "remove_opencti_label": ({"label_name": "triaged"}, "RemoveLabel", "object-label", "lbl-1"),
    "add_opencti_marking": (
        {"marking_definition_id": "marking-1"},
        "AddMarking",
        "object-marking",
        "marking-1",
    ),
    "remove_opencti_marking": (
        {"marking_definition_id": "marking-1"},
        "RemoveMarking",
        "object-marking",
        "marking-1",
    ),
}

_MUTATIONS = ("AddLabel", "RemoveLabel", "AddMarking", "RemoveMarking", "CreateLabel")


class _Stub:
    """``knowledge._graphql`` stand-in recording every call and its headers."""

    def __init__(self, target, mutation_error=None, lookup_error=None):
        self.target = target
        self.mutation_error = mutation_error
        self.lookup_error = lookup_error
        self.calls: list[tuple[str, dict | None, dict]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables, dict(headers)))
        label, _root = _operation_names(query)
        if label == "LabelByName":
            return {"label": {"id": "lbl-1", "value": "triaged"}}, None
        if label == "RefTarget":
            if self.lookup_error:
                return {}, self.lookup_error
            return {"stixObjectOrStixRelationship": self.target}, None
        if label in _MUTATIONS:
            if self.mutation_error:
                return {}, self.mutation_error
            return {}, None
        if label == "ReadEntity":
            return {}, None
        raise AssertionError(f"unexpected query {label}")

    def operations(self) -> list[tuple[str, str]]:
        return [_operation_names(q) for q, _v, _h in self.calls]

    def sent(self, operation: str) -> list[tuple[str, dict | None]]:
        return [(q, v) for q, v, _h in self.calls if _operation_names(q)[0] == operation]


def _target(kind: str) -> dict:
    entity_type, parents, _root = TARGETS[kind]
    return {"id": f"{kind}--1", "entity_type": entity_type, "parent_types": parents}


def _executor(monkeypatch, stub):
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={"opencti-draft-id": DRAFT})
    return executor


async def _run(executor, tool, arguments):
    return await executor.execute_tool(ToolCall(id=f"call-{tool}", name=tool, arguments=arguments))


@pytest.mark.parametrize("kind", sorted(TARGETS))
@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_each_kind_is_written_through_its_own_edit_root(monkeypatch, tool, kind):
    extra, operation, relationship_type, to_id = TOOLS[tool]
    root = TARGETS[kind][2]
    element_id = f"{kind}--1"
    stub = _Stub(_target(kind))
    executor = _executor(monkeypatch, stub)

    result = await _run(executor, tool, {"entity_id": element_id, **extra})

    sent = stub.sent(operation)
    assert len(sent) == 1
    query, variables = sent[0]
    assert _operation_names(query) == (operation, root)
    if operation.startswith("Add"):
        assert variables == {
            "id": element_id,
            "input": {"toId": to_id, "relationship_type": relationship_type},
        }
    else:
        assert variables == {
            "id": element_id,
            "toId": to_id,
            "relationship_type": relationship_type,
        }
    assert [r for _l, r in stub.operations() if r == "stixDomainObjectEdit"] == []
    assert not result.is_error, result.content
    assert result.content.endswith(f" {_NOUN[root]} {element_id}.")


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_the_lookup_and_the_write_stay_in_the_draft(monkeypatch, tool):
    extra = TOOLS[tool][0]
    stub = _Stub(_target("domain-name"))
    executor = _executor(monkeypatch, stub)

    await _run(executor, tool, {"entity_id": "domain-name--1", **extra})

    assert [h.get("opencti-draft-id") for _q, _v, h in stub.calls] == [DRAFT] * len(stub.calls)
    lookups = stub.sent("RefTarget")
    assert len(lookups) == 1
    query, variables = lookups[0]
    assert _operation_names(query) == ("RefTarget", "stixObjectOrStixRelationship")
    assert variables == {"id": "domain-name--1"}


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_an_opencti_refusal_is_returned_verbatim(monkeypatch, tool):
    extra, operation, _rel, _to = TOOLS[tool]
    refusal = f"GraphQL error in '{operation}': Element can not be loaded"
    stub = _Stub(_target("resolves-to"), mutation_error=refusal)
    executor = _executor(monkeypatch, stub)

    result = await _run(executor, tool, {"entity_id": "resolves-to--1", **extra})

    assert result.content == refusal
    assert result.is_error
    assert [label for label, _r in stub.operations()][-1] == operation


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_a_lookup_failure_is_returned_before_any_write(monkeypatch, tool):
    extra = TOOLS[tool][0]
    lookup_error = "GraphQL error in 'RefTarget': boom"
    stub = _Stub(_target("note"), lookup_error=lookup_error)
    executor = _executor(monkeypatch, stub)

    result = await _run(executor, tool, {"entity_id": "note--1", **extra})

    assert result.content == lookup_error
    assert [label for label, _r in stub.operations() if label in _MUTATIONS] == []


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_a_missing_element_changes_nothing(monkeypatch, tool):
    """The id is not echoed: the draft gate drops the draft on an error naming
    a "locked" one, and a model may pass a name such as 'blocked-ips'."""
    extra = TOOLS[tool][0]
    stub = _Stub(None)
    executor = _executor(monkeypatch, stub)

    result = await _run(executor, tool, {"entity_id": "blocked-ips", **extra})
    assert result.content == (
        "Error: no entity, observable, relationship or sighting has this ID "
        "in OpenCTI. Nothing was changed."
    )
    assert result.is_error
    assert [label for label, _r in stub.operations() if label in _MUTATIONS] == []

    # The draft is kept: the next call is still sent inside it.
    await _run(executor, tool, {"entity_id": "blocked-ips", **extra})
    assert stub.calls[-1][2].get("opencti-draft-id") == DRAFT


@pytest.mark.parametrize(
    "element, shown",
    [
        (
            {"id": "l-1", "entity_type": "Label", "parent_types": ["Basic-Object", "Stix-Object", "Stix-Meta-Object"]},
            " (type Label)",
        ),
        (
            {
                "id": "r-1",
                "entity_type": "object-label",
                "parent_types": ["basic-relationship", "stix-relationship", "stix-ref-relationship"],
            },
            " (type object-label)",
        ),
        # OpenCTI's own value, shown only when it is a plain type name.
        ({"id": "w-1", "entity_type": "Work\nspace", "parent_types": ["Basic-Object"]}, ""),
    ],
    ids=["label", "ref-relationship", "unprintable-type"],
)
async def test_an_element_that_carries_no_label_is_refused(monkeypatch, element, shown):
    stub = _Stub(element)
    executor = _executor(monkeypatch, stub)

    result = await _run(executor, "add_opencti_label", {"entity_id": element["id"], "label_name": "triaged"})

    assert result.content == (
        f"Error: this element{shown} cannot carry a label: only entities, "
        "observables, relationships and sightings can. Nothing was changed."
    )
    assert result.is_error
    assert [label for label, _r in stub.operations() if label in _MUTATIONS] == []


@pytest.mark.parametrize(
    "kind, read_field",
    [
        ("domain-name", "stixCoreObject"),
        ("resolves-to", "stixCoreRelationship"),
        ("sighting", None),
    ],
)
async def test_add_label_reads_the_element_back_through_its_own_query(monkeypatch, kind, read_field):
    stub = _Stub(_target(kind))
    executor = _executor(monkeypatch, stub)

    await _run(executor, "add_opencti_label", {"entity_id": f"{kind}--1", "label_name": "triaged"})

    reads = [(_operation_names(q)[1], v) for q, v in stub.sent("ReadEntity")]
    assert reads == ([(read_field, {"id": f"{kind}--1"})] if read_field else [])


async def test_add_label_returns_the_updated_relationship(monkeypatch):
    class _WithRelationship(_Stub):
        async def __call__(self, client, base_url, headers, query, variables=None):
            if _operation_names(query) == ("ReadEntity", "stixCoreRelationship"):
                self.calls.append((query, variables, dict(headers)))
                return {
                    "stixCoreRelationship": {
                        "id": "resolves-to--1",
                        "relationship_type": "resolves-to",
                        "from": {"entity_type": "Domain-Name", "observable_value": "evil.test"},
                        "to": {"entity_type": "IPv4-Addr", "observable_value": "192.0.2.1"},
                    }
                }, None
            return await super().__call__(client, base_url, headers, query, variables)

    stub = _WithRelationship(_target("resolves-to"))
    executor = _executor(monkeypatch, stub)

    result = await _run(executor, "add_opencti_label", {"entity_id": "resolves-to--1", "label_name": "triaged"})

    assert result.content.startswith(
        "Label 'triaged' added. Updated relationship:\n\n"
        "[Domain-Name] evil.test --resolves-to--> [IPv4-Addr] 192.0.2.1\n"
        "  ID: resolves-to--1\n"
    )


@pytest.mark.parametrize(
    "parents, entity_type, root",
    [(parents, entity_type, root) for entity_type, parents, root in TARGETS.values()]
    + [
        (["Basic-Object", "Stix-Object", "Stix-Meta-Object"], "Marking-Definition", None),
        (["Basic-Object", "Internal-Object"], "Workspace", None),
        (["basic-relationship", "stix-relationship", "stix-ref-relationship"], "object", None),
        ([], "", None),
    ],
    ids=[*TARGETS, "marking", "internal", "ref-relationship", "empty"],
)
def test_ref_edit_root(parents, entity_type, root):
    assert _ref_edit_root({"entity_type": entity_type, "parent_types": parents}) == root
