"""Bulk skill import endpoints — review pre-flight and per-skill import."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.api.v1.agent_skills as agent_skills_mod


def _skill_md(name: str, description: str, body: str = "Do work") -> bytes:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{body}\n"
    ).encode()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, payload in entries.items():
            zf.writestr(path, payload)
    return buf.getvalue()


class _UploadStub:
    def __init__(self, filename: str, payload: bytes):
        self.filename = filename
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


def _user():
    return SimpleNamespace(id=uuid4(), is_admin=True, role="admin", full_name="Tester")


def _archive() -> bytes:
    return _zip(
        {
            "alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha"),
            "beta-skill/beta-skill.md": _skill_md("beta-skill", "Beta"),
            "bad/SKILL.md": _skill_md("Bad Name!", "Bad"),
            "README.md": b"# readme\n",
        }
    )


def _patch_mapping(monkeypatch):
    monkeypatch.setattr(
        agent_skills_mod,
        "_normalize_resource_mappings",
        lambda **_kwargs: (
            {"schema_version": 1, "builtins": [], "scripts": [], "references": []},
            [],
        ),
    )
    monkeypatch.setattr(agent_skills_mod, "_missing_builtin_ids", lambda _manifest: [])


@pytest.mark.asyncio
async def test_validate_bulk_package_reports_found_and_ignored(monkeypatch):
    _patch_mapping(monkeypatch)
    monkeypatch.setattr(
        agent_skills_mod, "_visible_skill_names", AsyncMock(return_value={"alpha-skill"})
    )

    result = await agent_skills_mod.validate_skill_bulk_package(
        file=_UploadStub("skills.zip", _archive()),
        user=_user(),
        db=AsyncMock(),
    )

    assert result.valid is True
    assert result.skill_count == 3
    assert result.importable_count == 2
    assert result.ignored_count == 1
    assert [entry.reason for entry in result.ignored] == ["no_frontmatter"]

    by_path = {entry.path: entry for entry in result.skills}
    # A name the user already owns is flagged so the review screen can offer
    # skip / replace / import-as-new.
    assert by_path["alpha-skill/SKILL.md"].collision is True
    assert by_path["beta-skill/beta-skill.md"].collision is False
    assert by_path["bad/SKILL.md"].importable is False
    assert by_path["bad/SKILL.md"].errors


@pytest.mark.asyncio
async def test_validate_bulk_package_flags_unavailable_builtins_before_import(monkeypatch):
    monkeypatch.setattr(
        agent_skills_mod,
        "_normalize_resource_mappings",
        lambda **_kwargs: (
            {"schema_version": 1, "builtins": ["missing_tool"], "scripts": [], "references": []},
            [],
        ),
    )
    monkeypatch.setattr(agent_skills_mod, "_missing_builtin_ids", lambda _manifest: ["missing_tool"])
    monkeypatch.setattr(agent_skills_mod, "_visible_skill_names", AsyncMock(return_value=set()))

    result = await agent_skills_mod.validate_skill_bulk_package(
        file=_UploadStub("skills.zip", _zip({"alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha")})),
        user=_user(),
        db=AsyncMock(),
    )

    assert result.importable_count == 0
    assert "missing_tool" in result.skills[0].errors[0]


@pytest.mark.asyncio
async def test_validate_bulk_package_returns_archive_error_without_raising():
    result = await agent_skills_mod.validate_skill_bulk_package(
        file=_UploadStub("skills.zip", b"not a zip"),
        user=_user(),
        db=AsyncMock(),
    )

    assert result.valid is False
    assert result.errors
    assert result.skills == []


@pytest.mark.asyncio
async def test_import_bulk_package_imports_only_the_selected_skills(monkeypatch):
    imported: list[str] = []

    async def _apply(_db, *, request, user, parsed, on_collision, new_name=None):
        imported.append(parsed.name)
        return SimpleNamespace(id=uuid4(), name=parsed.name), True, []

    monkeypatch.setattr(agent_skills_mod, "_apply_package_import", _apply)
    monkeypatch.setattr(agent_skills_mod, "_run_package_import_side_effects", AsyncMock())

    db = AsyncMock()
    result = await agent_skills_mod.import_skill_bulk_package(
        request=MagicMock(),
        file=_UploadStub("skills.zip", _archive()),
        selected_paths='["alpha-skill/SKILL.md"]',
        on_collision="skip",
        collision_overrides=None,
        user=_user(),
        db=db,
    )

    assert imported == ["alpha-skill"]
    assert result.created_count == 1
    # Everything the user did not approve is reported as skipped, never
    # silently dropped from the response.
    statuses = {item.path: item.status for item in result.results}
    assert statuses["beta-skill/beta-skill.md"] == "skipped"
    assert statuses["bad/SKILL.md"] == "skipped"
    assert result.skipped_count == 2


@pytest.mark.asyncio
async def test_import_bulk_package_keeps_earlier_skills_when_a_later_one_fails(monkeypatch):
    calls: list[str] = []

    async def _apply(_db, *, request, user, parsed, on_collision, new_name=None):
        calls.append(parsed.name)
        if parsed.name == "beta-skill":
            raise HTTPException(status_code=400, detail="boom")
        return SimpleNamespace(id=uuid4(), name=parsed.name), True, []

    monkeypatch.setattr(agent_skills_mod, "_apply_package_import", _apply)
    monkeypatch.setattr(agent_skills_mod, "_run_package_import_side_effects", AsyncMock())

    db = AsyncMock()
    result = await agent_skills_mod.import_skill_bulk_package(
        request=MagicMock(),
        file=_UploadStub("skills.zip", _archive()),
        selected_paths=None,
        on_collision="skip",
        collision_overrides=None,
        user=_user(),
        db=db,
    )

    assert calls == ["alpha-skill", "beta-skill"]
    # The skill that landed before the failure was committed and stays.
    assert db.commit.await_count == 1
    assert db.rollback.await_count == 1
    outcomes = {item.path: (item.status, item.message) for item in result.results}
    assert outcomes["alpha-skill/SKILL.md"][0] == "created"
    assert outcomes["beta-skill/beta-skill.md"] == ("failed", "boom")
    assert result.created_count == 1
    assert result.failed_count == 1


@pytest.mark.asyncio
async def test_a_non_importable_skill_only_fails_when_it_was_explicitly_selected(monkeypatch):
    """Omitting the selection means "import what can be imported", so an entry
    that was never importable must not be reported as a failure."""

    async def _apply(_db, *, request, user, parsed, on_collision, new_name=None):
        return SimpleNamespace(id=uuid4(), name=parsed.name), True, []

    monkeypatch.setattr(agent_skills_mod, "_apply_package_import", _apply)
    monkeypatch.setattr(agent_skills_mod, "_run_package_import_side_effects", AsyncMock())

    implicit = await agent_skills_mod.import_skill_bulk_package(
        request=MagicMock(),
        file=_UploadStub("skills.zip", _archive()),
        selected_paths=None,
        on_collision="skip",
        collision_overrides=None,
        user=_user(),
        db=AsyncMock(),
    )
    broken = next(item for item in implicit.results if item.path == "bad/SKILL.md")
    assert broken.status == "skipped"
    # The reason is kept even when the entry is only skipped.
    assert broken.message
    assert implicit.failed_count == 0

    explicit = await agent_skills_mod.import_skill_bulk_package(
        request=MagicMock(),
        file=_UploadStub("skills.zip", _archive()),
        selected_paths='["bad/SKILL.md"]',
        on_collision="skip",
        collision_overrides=None,
        user=_user(),
        db=AsyncMock(),
    )
    broken = next(item for item in explicit.results if item.path == "bad/SKILL.md")
    assert broken.status == "failed"
    assert explicit.failed_count == 1


@pytest.mark.asyncio
async def test_import_bulk_package_skip_policy_turns_a_collision_into_a_skip(monkeypatch):
    seen_policies: list[str] = []

    async def _apply(_db, *, request, user, parsed, on_collision, new_name=None):
        seen_policies.append(on_collision)
        raise HTTPException(status_code=409, detail="A skill named 'alpha-skill' already exists.")

    monkeypatch.setattr(agent_skills_mod, "_apply_package_import", _apply)
    monkeypatch.setattr(agent_skills_mod, "_run_package_import_side_effects", AsyncMock())

    result = await agent_skills_mod.import_skill_bulk_package(
        request=MagicMock(),
        file=_UploadStub("skills.zip", _zip({"alpha-skill/SKILL.md": _skill_md("alpha-skill", "Alpha")})),
        selected_paths=None,
        on_collision="skip",
        collision_overrides=None,
        user=_user(),
        db=AsyncMock(),
    )

    # `skip` reuses the single-import `cancel` semantics under the hood.
    assert seen_policies == ["cancel"]
    assert result.skipped_count == 1
    assert result.failed_count == 0


@pytest.mark.asyncio
async def test_import_bulk_package_applies_per_skill_collision_overrides(monkeypatch):
    policies: dict[str, str] = {}

    async def _apply(_db, *, request, user, parsed, on_collision, new_name=None):
        policies[parsed.name] = on_collision
        return SimpleNamespace(id=uuid4(), name=parsed.name), on_collision != "replace", []

    monkeypatch.setattr(agent_skills_mod, "_apply_package_import", _apply)
    monkeypatch.setattr(agent_skills_mod, "_run_package_import_side_effects", AsyncMock())

    result = await agent_skills_mod.import_skill_bulk_package(
        request=MagicMock(),
        file=_UploadStub("skills.zip", _archive()),
        selected_paths='["alpha-skill/SKILL.md", "beta-skill/beta-skill.md"]',
        on_collision="skip",
        collision_overrides='{"alpha-skill/SKILL.md": "replace"}',
        user=_user(),
        db=AsyncMock(),
    )

    assert policies == {"alpha-skill": "replace", "beta-skill": "cancel"}
    assert result.replaced_count == 1
    assert result.created_count == 1


@pytest.mark.asyncio
async def test_import_bulk_package_rejects_invalid_inputs():
    with pytest.raises(HTTPException) as invalid_policy:
        await agent_skills_mod.import_skill_bulk_package(
            request=MagicMock(),
            file=_UploadStub("skills.zip", _archive()),
            selected_paths=None,
            on_collision="cancel",
            collision_overrides=None,
            user=_user(),
            db=AsyncMock(),
        )
    assert invalid_policy.value.status_code == 422

    with pytest.raises(HTTPException) as bad_json:
        await agent_skills_mod.import_skill_bulk_package(
            request=MagicMock(),
            file=_UploadStub("skills.zip", _archive()),
            selected_paths="{not json",
            on_collision="skip",
            collision_overrides=None,
            user=_user(),
            db=AsyncMock(),
        )
    assert bad_json.value.status_code == 422

    with pytest.raises(HTTPException) as unknown_path:
        await agent_skills_mod.import_skill_bulk_package(
            request=MagicMock(),
            file=_UploadStub("skills.zip", _archive()),
            selected_paths='["nope/SKILL.md"]',
            on_collision="skip",
            collision_overrides=None,
            user=_user(),
            db=AsyncMock(),
        )
    assert unknown_path.value.status_code == 422
