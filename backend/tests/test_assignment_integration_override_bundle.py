"""An assignment's integration override across export and import.

``Assignment.integration_bindings`` is tri-state - NULL inherits every
integration of the agent, ``{}`` means none, a subset names integration ids -
and the bundle used to drop it while the import wrote NULL: an assignment set
to "no integrations" came back inheriting every integration later bound to the
agent. The rule is FAIL CLOSED, on every path that builds or reads a bundle:

- "none" exports and imports as none;
- a subset (whose ids never exist on the importing install) imports as none,
  with a line in the import report naming the assignment;
- "inherit" stays inherit, and so does a bundle that predates the key.

The bundle is built by the real export builder and read back by the shared
import pipeline, as a NON-admin user.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from app.api.v1 import agent_import as agent_import_api
from app.models.agent import Agent
from app.models.agent_copy import (
    ASSIGNMENT_INTEGRATION_OVERRIDE_KEY,
    build_assignment_copy_kwargs,
    export_integration_override,
    import_integration_override,
    strip_internal_ids_for_public_download,
)
from app.models.assignment import Assignment
from app.services import agent_bundle_projection
from app.services.agentschema import build_agentschema_export, prepare_agent_import


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _BundleDB:
    """Answers each ``select(Model)`` with its rows; records what is added."""

    def __init__(self, rows_by_model=None):
        self._rows = rows_by_model or {}
        self.added: list[object] = []

    async def execute(self, stmt):
        return _Result(self._rows.get(stmt.column_descriptions[0].get("entity"), []))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


_NON_ADMIN = SimpleNamespace(
    id=uuid.uuid4(), email="u@example.com", full_name="User", is_admin=False, role="user"
)


def _agent_with_three_overrides():
    owner = uuid.uuid4()
    agent = Agent(id=uuid.uuid4(), user_id=owner, name="Scoped", persona="p")
    integration = str(uuid.uuid4())

    def _assignment(name, bindings):
        return Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=owner,
            name=name,
            trigger_type="manual",
            trigger_config={},
            prompt="p",
            integration_bindings=bindings,
        )

    assignments = [
        _assignment("Inherits", None),
        _assignment("Uses none", {}),
        _assignment("GitHub only", {"github": [integration]}),
    ]
    return agent, assignments, integration


async def _export(agent, assignments):
    return await agent_bundle_projection.build_agent_export_bundle(
        _BundleDB({Assignment: assignments}), agent
    )


async def _import(monkeypatch, bundle):
    async def _unique_slug(_db, name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)
    db = _BundleDB()
    imported = await agent_import_api.create_agent_from_bundle(
        db, user=_NON_ADMIN, bundle=bundle
    )
    assignments = {a.name: a for a in db.added if isinstance(a, Assignment)}
    return imported, assignments


def test_the_bundle_carries_the_mode_and_provider_names_never_ids():
    integration = str(uuid.uuid4())
    assert export_integration_override(None) == {"mode": "inherit"}
    assert export_integration_override({}) == {"mode": "none"}
    assert export_integration_override({"slack": [integration], "github": integration}) == {
        "mode": "subset",
        "providers": ["github", "slack"],
    }
    # A malformed column value fails closed.
    assert export_integration_override(["not", "a", "map"]) == {"mode": "none"}


async def test_an_export_import_round_trip_never_widens_an_assignment(monkeypatch):
    agent, assignments, integration = _agent_with_three_overrides()
    bundle = await _export(agent, assignments)

    exported = {a["name"]: a[ASSIGNMENT_INTEGRATION_OVERRIDE_KEY] for a in bundle["assignments"]}
    assert exported == {
        "Inherits": {"mode": "inherit"},
        "Uses none": {"mode": "none"},
        "GitHub only": {"mode": "subset", "providers": ["github"]},
    }
    assert integration not in json.dumps(bundle)

    imported, created = await _import(monkeypatch, bundle)

    assert created["Inherits"].integration_bindings is None
    assert created["Uses none"].integration_bindings == {}
    # A subset cannot be rebuilt here: it arrives as "none", and says so.
    assert created["GitHub only"].integration_bindings == {}
    [note] = imported.warnings
    assert "'GitHub only'" in note and "github" in note and "re-select" in note


async def test_a_bundle_that_predates_the_key_keeps_importing_as_inherit(monkeypatch):
    agent, assignments, _ = _agent_with_three_overrides()
    bundle = await _export(agent, assignments)
    for entry in bundle["assignments"]:
        del entry[ASSIGNMENT_INTEGRATION_OVERRIDE_KEY]

    imported, created = await _import(monkeypatch, bundle)

    assert {a.integration_bindings for a in created.values()} == {None}
    assert imported.warnings == []


def test_an_unrecognised_override_fails_closed_and_says_so():
    bindings, note = import_integration_override(
        {ASSIGNMENT_INTEGRATION_OVERRIDE_KEY: {"mode": "everything"}}, "Odd"
    )
    assert bindings == {}
    assert note and "'Odd'" in note
    # An explicit null reads as an absent key: the bundle predates the field.
    assert import_integration_override({ASSIGNMENT_INTEGRATION_OVERRIDE_KEY: None}, "x") == (
        None,
        None,
    )


async def test_the_file_import_and_agentschema_paths_follow_the_same_rule(monkeypatch):
    agent, assignments, _ = _agent_with_three_overrides()
    bundle = await _export(agent, assignments)

    native = prepare_agent_import(json.dumps(bundle).encode("utf-8"), filename="agent.json")
    yaml_text, _document = build_agentschema_export(bundle)
    agentschema = prepare_agent_import(yaml_text.encode("utf-8"), filename="agent.yaml")

    for prepared in (native, agentschema):
        _imported, created = await _import(monkeypatch, prepared.bundle)
        assert created["Uses none"].integration_bindings == {}
        assert created["GitHub only"].integration_bindings == {}
        assert created["Inherits"].integration_bindings is None


async def test_the_public_catalog_download_carries_the_mode_without_ids(monkeypatch):
    agent, assignments, integration = _agent_with_three_overrides()
    download = strip_internal_ids_for_public_download(await _export(agent, assignments))

    assert integration not in json.dumps(download)
    _imported, created = await _import(monkeypatch, download)
    assert created["Uses none"].integration_bindings == {}
    assert created["GitHub only"].integration_bindings == {}
    assert created["Inherits"].integration_bindings is None


def test_an_in_place_duplicate_keeps_the_override_as_is():
    """Same install, same integrations: the duplicate keeps the ids the source
    names (the catalog clone re-keys them, pinned in test_catalog_clone.py) -
    neither path goes through the bundle, and neither widens."""
    _agent, assignments, integration = _agent_with_three_overrides()
    copies = {a.name: build_assignment_copy_kwargs(a)["integration_bindings"] for a in assignments}
    assert copies == {
        "Inherits": None,
        "Uses none": {},
        "GitHub only": {"github": [integration]},
    }
