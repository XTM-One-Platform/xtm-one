"""``GET /skills/{id}/export`` for a skill whose SKILL.md lacks name/description.

The skill keeps both in its own properties; the export writes them into the
root SKILL.md and every version snapshot, and the response's readiness flag
judges the skill the same way.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.api.v1.agent_skills as agent_skills_mod
from app.services.skill_package_manager import parse_skill_package_zip

BARE_BODY = "Review the diff.\n\n## Steps\n"


def _user():
    return SimpleNamespace(id=uuid4(), is_admin=False, role="user", email="ada@example.com")


def _skill(**overrides):
    base = {
        "id": uuid4(),
        "user_id": uuid4(),
        "url": None,
        "source_integration_id": None,
        "source_ref": None,
        "name": "Code Review",
        "description": "Reviews a diff",
        "compatibility": None,
        "license": None,
        "content": BARE_BODY,
        "fetched_at": datetime.now(UTC),
        "refresh_failure_count": 0,
        "auth_failure_count": 0,
        "tags": [],
        "enabled": True,
        "hide_from_chat": False,
        "is_company_managed": False,
        "is_public_catalog": False,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "access_restricted": False,
        "forked_from": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _version(number: int, content: str, description: str | None):
    return SimpleNamespace(
        version_number=number,
        content=content,
        description=description,
        compatibility=None,
        created_at=None,
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return MagicMock(all=lambda: self._rows)


class _DB:
    """Answers export_skill's queries in order: the skill, the agents, the versions."""

    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, _statement):
        return _Result(self._results.pop(0))


@pytest.fixture(autouse=True)
def _no_groups_no_resources(monkeypatch):
    async def _groups(_db, _uid):
        return []

    async def _resources(_db, *, skill_id):
        return []

    monkeypatch.setattr(agent_skills_mod, "get_user_group_ids", _groups)
    monkeypatch.setattr(agent_skills_mod, "_list_skill_resources", _resources)


async def _export(skill, versions) -> bytes:
    response = await agent_skills_mod.export_skill(
        str(skill.id),
        format="zip",
        include_versionning=True,
        user=_user(),
        db=_DB([skill], [], versions),
    )
    return b"".join([chunk async for chunk in response.body_iterator])


@pytest.mark.asyncio
async def test_export_completes_the_root_and_the_version_snapshots():
    skill = _skill()
    raw = await _export(
        skill,
        [_version(1, "Older text\n", "Older description"), _version(2, BARE_BODY, "Reviews a diff")],
    )
    parsed = parse_skill_package_zip(raw)
    assert (parsed.name, parsed.description) == ("code-review", "Reviews a diff")
    assert [v.description for v in parsed.versions] == ["Older description", "Reviews a diff"]
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert "code-review/SKILL.md" in names


@pytest.mark.asyncio
async def test_export_without_any_version_row_completes_the_single_snapshot():
    parsed = parse_skill_package_zip(await _export(_skill(), []))
    assert [v.number for v in parsed.versions] == [1]
    assert parsed.versions[0].description == "Reviews a diff"


@pytest.mark.asyncio
async def test_export_keeps_a_frontmatter_the_author_wrote():
    content = "---\nname: kept-name\ndescription: kept description\n---\n\nBody\n"
    parsed = parse_skill_package_zip(await _export(_skill(content=content), []))
    assert (parsed.name, parsed.description) == ("kept-name", "kept description")
    assert parsed.root_content == content


@pytest.mark.asyncio
async def test_export_refuses_a_skill_whose_name_gives_no_package_name():
    skill = _skill(name="日本語")
    with pytest.raises(HTTPException) as err:
        await _export(skill, [])
    assert err.value.status_code == 422


def test_response_reports_such_a_skill_as_exportable():
    response = agent_skills_mod._to_response(_skill(), can_manage=False)
    assert response.package_exportable is True
    assert response.frontmatter_warnings == []


def test_response_keeps_the_warning_when_the_properties_cannot_fill_the_gap():
    response = agent_skills_mod._to_response(
        _skill(name="日本語", description=None), can_manage=False
    )
    assert response.package_exportable is False
    assert len(response.frontmatter_warnings) == 2
