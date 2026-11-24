"""Fleet documentation (#9174 slice 5): sections, fingerprint and the write rules, no database."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_docs
from app.services.fleet_docs import (
    DOC_SECTIONS,
    clean_sections,
    documentation_fingerprint,
    documentation_view,
    save_documentation,
    seed_documentation,
)
from app.services.fleets import FleetActionRefusedError


def _fleet(**fields) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "is_builtin": False,
        "documentation": None,
        "default_context_inclusion": "none",
        "default_collaboration_instruction": "",
        "organization_scope": {"mode": "primary"},
    }
    return SimpleNamespace(**{**base, **fields})


def _row(**fields) -> SimpleNamespace:
    base = {
        "agent_id": uuid.uuid4(),
        "target_fleet_id": None,
        "role": "member",
        "context_inclusion": None,
        "collaboration_instruction": None,
        "organization_scope": None,
    }
    return SimpleNamespace(**{**base, **fields})


@pytest.fixture
def fingerprint(monkeypatch):
    """current_fingerprint without a database: a value the test moves."""
    state = {"value": "f" * 16}

    async def fake(_db, _fleet):
        return state["value"]

    monkeypatch.setattr(fleet_docs, "current_fingerprint", fake)
    return state


def test_sections_keep_the_reading_order_and_drop_empty_ones():
    cleaned = clean_sections({"prerequisites": " OpenCTI connected ", "purpose": "Why", "flow": "   "})
    assert list(cleaned) == ["purpose", "prerequisites"]
    assert cleaned["prerequisites"] == "OpenCTI connected"


def test_an_unknown_section_or_a_text_too_long_is_refused():
    with pytest.raises(FleetActionRefusedError) as unknown:
        clean_sections({"purpose": "x", "secret": "y"})
    assert unknown.value.status_code == 422 and "secret" in unknown.value.detail
    with pytest.raises(FleetActionRefusedError) as long:
        clean_sections({"purpose": "x" * (fleet_docs.SECTION_MAX_CHARS + 1)})
    assert long.value.status_code == 422


def test_the_fingerprint_moves_with_the_structure_and_the_member_configuration():
    fleet = _fleet()
    row = _row()
    base = documentation_fingerprint({"members": []}, fleet, [row])
    assert documentation_fingerprint({"members": []}, fleet, [row]) == base
    assert documentation_fingerprint({"members": [{"slug": "x"}]}, fleet, [row]) != base
    row.collaboration_instruction = "Call me for CVEs"
    assert documentation_fingerprint({"members": []}, fleet, [row]) != base
    row.collaboration_instruction = None
    fleet.default_context_inclusion = "summary"
    assert documentation_fingerprint({"members": []}, fleet, [row]) != base


async def test_a_save_versions_the_documentation_and_keeps_the_previous_one(fingerprint):
    fleet = _fleet()
    author = uuid.uuid4()
    await save_documentation(None, fleet, {"purpose": "v1"}, author=author, base_version=0)
    await save_documentation(None, fleet, {"purpose": "v2"}, author=author, base_version=1)
    doc = fleet.documentation
    assert doc["version"] == 2 and doc["sections"] == {"purpose": "v2"}
    assert doc["updated_by"] == str(author)
    assert [h["version"] for h in doc["history"]] == [1]
    assert doc["history"][0]["sections"] == {"purpose": "v1"}


async def test_a_save_over_a_newer_version_is_refused(fingerprint):
    fleet = _fleet()
    await save_documentation(None, fleet, {"purpose": "v1"}, author=uuid.uuid4(), base_version=None)
    with pytest.raises(FleetActionRefusedError) as stale:
        await save_documentation(None, fleet, {"purpose": "mine"}, author=uuid.uuid4(), base_version=0)
    assert stale.value.status_code == 409
    assert fleet.documentation["sections"] == {"purpose": "v1"}


async def test_a_built_in_fleet_refuses_a_regenerate_and_a_version_restore(fingerprint):
    """#1956: the seed writes a built-in fleet's documentation; nobody saves or restores a version there."""
    fleet = _fleet(is_builtin=True)
    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    await seed_documentation(None, fleet, {"purpose": "Shipped, revised"})
    stored = dict(fleet.documentation)
    with pytest.raises(FleetActionRefusedError) as saved:
        await save_documentation(None, fleet, {"purpose": "Generated here"}, author=uuid.uuid4(), base_version=2)
    assert saved.value.status_code == 422
    with pytest.raises(FleetActionRefusedError) as restored:
        await fleet_docs.restore_documentation_version(None, fleet, 1, author=uuid.uuid4(), base_version=2)
    assert restored.value.status_code == 422
    assert fleet.documentation == stored


async def test_a_built_in_fleet_is_never_editable_nor_out_of_date(fingerprint):
    """#1956: the seed owns the structure, so a seed change to a member never flags the shipped documentation."""
    fleet = _fleet(is_builtin=True)
    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    fingerprint["value"] = "2" * 16  # a persona the seed rewrote at boot
    view = await documentation_view(None, fleet, can_edit=True)
    assert view.out_of_date is False and view.editable is False and view.local is False

    custom = _fleet()
    await save_documentation(None, custom, {"purpose": "Why"}, author=uuid.uuid4(), base_version=0)
    fingerprint["value"] = "3" * 16
    assert (await documentation_view(None, custom, can_edit=True)).out_of_date is True


async def test_the_boot_replaces_a_local_version_left_on_a_built_in_fleet(fingerprint):
    """#1956: a local version an earlier build wrote gives way to the shipped one, kept in the history."""
    fleet = _fleet(
        is_builtin=True,
        documentation={
            "sections": {"purpose": "Generated here"},
            "version": 1,
            "updated_by": str(uuid.uuid4()),
            "fingerprint": "0" * 16,
            "local": True,
            "shipped": {"sections": {"purpose": "Shipped"}, "structured": None},
            "history": [],
        },
    )
    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    doc = fleet.documentation
    assert doc["sections"] == {"purpose": "Shipped"} and doc["updated_by"] == "seed" and doc["version"] == 2
    assert "local" not in doc and doc["fingerprint"] == "f" * 16
    assert doc["history"][0]["sections"] == {"purpose": "Generated here"}
    assert (await documentation_view(None, fleet, can_edit=True)).out_of_date is False
    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    assert fleet.documentation["version"] == 2, "the next boot writes nothing new"


async def test_the_history_lists_the_current_then_the_kept_versions_and_a_kept_one_restores_as_next(fingerprint):
    """#9768 S2 B1: version history and Restore this version, saved as the next version."""
    fleet = _fleet()
    author = uuid.uuid4()
    await save_documentation(None, fleet, {"purpose": "v1"}, author=author, base_version=0)
    await save_documentation(None, fleet, {"purpose": "v2", "objects_created": "o2"}, author=author, base_version=1)
    await save_documentation(None, fleet, {"purpose": "v3"}, author=author, base_version=2)

    history = fleet_docs.documentation_history(fleet)
    assert [(h.version, h.current) for h in history] == [(3, True), (2, False), (1, False)]
    assert history[1].section_keys == ["purpose", "objects_created"]
    assert history[0].updated_by == str(author)

    await fleet_docs.restore_documentation_version(None, fleet, 1, author=author, base_version=3)
    doc = fleet.documentation
    assert doc["version"] == 4 and doc["sections"] == {"purpose": "v1"}
    assert [h["version"] for h in doc["history"]] == [3, 2, 1], "the replaced version stays restorable"
    assert fleet_docs.documentation_history(_fleet()) == []


async def test_a_restore_is_refused_when_stale_current_or_not_kept(fingerprint):
    fleet = _fleet()
    author = uuid.uuid4()
    await save_documentation(None, fleet, {"purpose": "v1"}, author=author, base_version=0)
    await save_documentation(None, fleet, {"purpose": "v2"}, author=author, base_version=1)
    for version, base, status in ((1, 1, 409), (2, 2, 422), (9, 2, 404)):
        with pytest.raises(FleetActionRefusedError) as refused:
            await fleet_docs.restore_documentation_version(None, fleet, version, author=author, base_version=base)
        assert refused.value.status_code == status
    assert fleet.documentation["version"] == 2


async def test_a_restore_records_where_its_text_came_from_for_each_author_kind(fingerprint):
    """#9768 S2 UX r2 B1: a restore reads "Restored from ...", never "Generated by AI"."""
    source = _fleet(is_builtin=True)
    author = uuid.uuid4()
    await seed_documentation(None, source, {"purpose": "Shipped"})
    await fleet_docs.restore_shipped_documentation(None, source, author=author)
    assert (await documentation_view(None, source, can_edit=True)).restored_from == {"version": None, "author": "seed"}

    fork = _fleet()
    await fleet_docs.copy_documentation(None, source, fork)
    await save_documentation(None, fork, {"purpose": "Mine"}, author=author, base_version=1)
    assert (await documentation_view(None, fork, can_edit=True)).restored_from is None
    await fleet_docs.restore_documentation_version(None, fork, 1, author=author, base_version=2)
    assert fork.documentation["restored_from"] == {"version": 1, "author": "fork"}
    await fleet_docs.restore_documentation_version(None, fork, 2, author=author, base_version=3)
    assert fork.documentation["restored_from"] == {"version": 2, "author": "ai"}
    # A restore of a restore keeps the author of the text it brings back.
    await fleet_docs.restore_documentation_version(None, fork, 3, author=author, base_version=4)
    assert fork.documentation["restored_from"] == {"version": 3, "author": "fork"}

    history = {h.version: h.restored_from for h in fleet_docs.documentation_history(fork)}
    assert history[5] == {"version": 3, "author": "fork"} and history[2] is None and history[1] is None
    assert history[4] == {"version": 2, "author": "ai"}


async def test_restore_shipped_is_refused_on_a_custom_fleet_or_without_a_shipped_copy(fingerprint):
    with pytest.raises(FleetActionRefusedError) as custom:
        await fleet_docs.restore_shipped_documentation(None, _fleet(), author=uuid.uuid4())
    assert custom.value.status_code == 422
    with pytest.raises(FleetActionRefusedError) as missing:
        await fleet_docs.restore_shipped_documentation(None, _fleet(is_builtin=True), author=uuid.uuid4())
    assert missing.value.status_code == 409


async def test_the_history_is_bounded(fingerprint):
    fleet = _fleet()
    for n in range(fleet_docs.HISTORY_KEPT + 3):
        await save_documentation(None, fleet, {"purpose": f"v{n}"}, author=uuid.uuid4(), base_version=None)
    assert len(fleet.documentation["history"]) == fleet_docs.HISTORY_KEPT


async def test_the_view_says_out_of_date_once_the_fleet_moved(fingerprint):
    fleet = _fleet()
    empty = await documentation_view(None, fleet, can_edit=True)
    assert empty.version == 0 and empty.out_of_date is None and empty.editable is True

    await save_documentation(None, fleet, {"purpose": "Why", "flow": "Who"}, author=uuid.uuid4(), base_version=0)
    fresh = await documentation_view(None, fleet, can_edit=False)
    assert fresh.out_of_date is False and fresh.editable is False
    assert list(fresh.sections) == ["purpose", "flow"]

    fingerprint["value"] = "0" * 16
    moved = await documentation_view(None, fleet, can_edit=True)
    assert moved.out_of_date is True
    # Never rewritten by a read.
    assert fleet.documentation["sections"] == {"purpose": "Why", "flow": "Who"}


async def test_the_seed_writes_once_and_follows_the_structure_without_a_new_version(fingerprint):
    fleet = _fleet(is_builtin=True)
    await seed_documentation(None, fleet, None)
    assert fleet.documentation is None

    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    assert fleet.documentation["version"] == 1 and fleet.documentation["updated_by"] == "seed"

    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    assert fleet.documentation["version"] == 1

    fingerprint["value"] = "1" * 16
    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    assert fleet.documentation["version"] == 1
    assert fleet.documentation["fingerprint"] == "1" * 16

    await seed_documentation(None, fleet, {"purpose": "Shipped, revised"})
    assert fleet.documentation["version"] == 2


async def test_a_fork_copies_the_documentation_as_its_version_1(fingerprint):
    source = _fleet(is_builtin=True)
    await seed_documentation(None, source, {"purpose": "Shipped", "objects_created": "Fixed CVEs"})
    await seed_documentation(None, source, {"purpose": "Shipped v2", "objects_created": "Fixed CVEs"})
    fork = _fleet()
    await fleet_docs.copy_documentation(None, source, fork)
    assert fork.documentation["version"] == 1
    assert fork.documentation["updated_by"] == "fork"
    assert fork.documentation["history"] == []
    assert fork.documentation["sections"] == {"purpose": "Shipped v2", "objects_created": "Fixed CVEs"}
    view = await documentation_view(None, fork, can_edit=True)
    assert view.editable is True and view.out_of_date is False


async def test_a_fork_of_an_undocumented_fleet_has_no_documentation(fingerprint):
    fork = _fleet()
    await fleet_docs.copy_documentation(None, _fleet(), fork)
    assert fork.documentation is None


_STRUCTURED = {
    "purpose_line": "Why",
    "triggers": [{"module": "opencti", "name": "New report", "filter": "A report is created"}],
    "steps": [{"title": "Map"}, {"title": "Test", "gate": True}],
    "prerequisites": [{"check": "integration:opencti", "label": "OpenCTI connected", "fix_route": "/integrations"}],
    "quota": [{"trigger": "New report", "runs_min": 1, "runs_max": 4}],
}


def test_structured_parts_are_validated_and_trimmed():
    cleaned = fleet_docs.clean_structured(_STRUCTURED)
    assert cleaned["steps"] == [{"title": "Map"}, {"title": "Test", "gate": True}]
    assert "gates" not in cleaned
    assert fleet_docs.clean_structured({}) is None and fleet_docs.clean_structured(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        # Every integration provider is a module since #9979 (jira included); an unknown key is still refused.
        {"triggers": [{"module": "mars", "name": "x"}]},
        {"steps": [{"title": "x", "colour": "red"}]},
        # Every catalog provider is a connected check since UX r1 B2 on #9988 (jira included); an unknown one is not.
        {"prerequisites": [{"check": "integration:mars", "label": "x"}]},
        {"prerequisites": [{"check": "integration:schedule", "label": "x"}]},
        {"prerequisites": [{"check": "manual", "label": "x", "fix_route": "https://evil.example"}]},
        {"quota": [{"trigger": "x", "runs_min": 5, "runs_max": 1}]},
        {"unknown": 1},
    ],
)
def test_an_invalid_structured_part_is_refused(bad):
    with pytest.raises(FleetActionRefusedError) as refused:
        fleet_docs.clean_structured(bad)
    assert refused.value.status_code == 422


async def test_a_save_keeps_the_structured_parts_unless_it_sends_them(fingerprint):
    fleet = _fleet()
    author = uuid.uuid4()
    await save_documentation(None, fleet, {"purpose": "v1"}, author=author, base_version=0, structured=_STRUCTURED)
    assert fleet.documentation["structured"]["purpose_line"] == "Why"
    # An editor that sends only the markdown keeps the structured parts.
    await save_documentation(None, fleet, {"purpose": "v2"}, author=author, base_version=1)
    assert fleet.documentation["structured"]["purpose_line"] == "Why"
    assert fleet.documentation["history"][0]["structured"]["purpose_line"] == "Why"
    view = await documentation_view(None, fleet, can_edit=True)
    assert view.structured["steps"][1]["gate"] is True
    # {} clears them.
    await save_documentation(None, fleet, {"purpose": "v3"}, author=author, base_version=2, structured={})
    assert "structured" not in fleet.documentation
    assert (await documentation_view(None, fleet, can_edit=True)).structured is None


async def test_the_seed_writes_a_new_version_when_only_the_structured_parts_change(fingerprint):
    fleet = _fleet(is_builtin=True)
    await seed_documentation(None, fleet, {"purpose": "Shipped"})
    await seed_documentation(None, fleet, {"purpose": "Shipped"}, _STRUCTURED)
    assert fleet.documentation["version"] == 2
    await seed_documentation(None, fleet, {"purpose": "Shipped"}, _STRUCTURED)
    assert fleet.documentation["version"] == 2


async def test_a_fork_copies_the_structured_parts(fingerprint):
    source = _fleet(is_builtin=True)
    await seed_documentation(None, source, {"purpose": "Shipped"}, _STRUCTURED)
    fork = _fleet()
    await fleet_docs.copy_documentation(None, source, fork)
    assert fork.documentation["structured"] == source.documentation["structured"]
    assert fork.documentation["structured"] is not source.documentation["structured"]


def test_every_section_key_is_a_lower_case_identifier():
    assert len(DOC_SECTIONS) == len(set(DOC_SECTIONS))
    assert all(k.isidentifier() and k == k.lower() for k in DOC_SECTIONS)
